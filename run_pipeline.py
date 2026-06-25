"""LedgerLens detection pipeline entry point.

Loads recent trades, computes Benford + ML features per wallet/asset pair,
scores each with the trained ensemble, and publishes the resulting
`RiskScore` records to ledgerlens-api (and optionally ledgerlens-contracts).
See README.md's "LedgerLens Organization" section for how this fits with
the other repos in the org.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Optional
import pandas as pd

from config.settings import settings
from detection.cross_pair_engine import (
    build_volume_time_series,
    find_correlated_pairs,
    find_cross_pair_wallets,
)
from detection.drift_monitor import record_scored_features
from detection.feature_engineering import build_feature_vector
from detection.feature_store import FeatureStore
from detection.graph_engine import build_ring_membership_index, build_transaction_graph, find_wash_rings
from detection.model_inference import load_calibration, load_models, score_feature_matrix, score_feature_vector, score_with_uncertainty
from detection.path_cycle_detector import detect_cycles_from_payments, path_payment_cycles_to_alerts
from detection.path_payment_engine import detect_atomic_circular_routes
from detection.risk_score import RiskScore
from detection.storage import (
    save_alerts,
    save_circular_routes,
    save_feature_vectors,
    save_liquidity_pool_trades,
    save_pair_correlations,
    save_path_payment_cycles,
    save_path_payments,
    save_rings,
    save_scores,
    promote_cold_to_hot,
)
from detection.shap_explainer import explain_score, top_contributing_features
from ingestion.account_loader import async_load_account_metadata, load_account_metadata
from ingestion.data_models import Trade, TradeType
from ingestion.historical_loader import async_load_historical_trades, load_historical_trades
from ingestion.horizon_streamer import stream_trades_with_cursor
from ingestion.http_client import AsyncHorizonClient
from ingestion.operations_loader import (
    async_load_order_book_events_for_pair,
    load_order_book_events_for_pair,
)
from ingestion.path_payment_loader import async_load_path_payments, load_path_payments_for_accounts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ledgerlens.pipeline")

# Global feature store instance
_feature_store: Optional[FeatureStore] = None
_last_cold_flush_time = 0.0


def _get_feature_store() -> FeatureStore:
    """Lazy initialization of global feature store."""
    global _feature_store
    if _feature_store is None:
        _feature_store = FeatureStore()
    return _feature_store


def _maybe_flush_feature_store_to_cold() -> None:
    """Periodically flush hot feature states to cold storage (SQLite)."""
    global _last_cold_flush_time
    now = time.time()
    flush_interval = settings.feature_store_flush_interval_seconds
    
    if now - _last_cold_flush_time >= flush_interval:
        try:
            fs = _get_feature_store()
            count = promote_cold_to_hot(fs)
            if count > 0:
                logger.debug(f"Promoted {count} feature states from cold to hot storage")
            _last_cold_flush_time = now
        except Exception as e:
            logger.warning(f"Failed to flush feature store to cold storage: {e}")


def adjust_score_with_temporal(account: str, pair_key: str, score: RiskScore, models: dict) -> None:
    temporal_model = models.get("temporal_lstm")
    if temporal_model is None:
        return

    from detection.temporal_dataset import build_score_sequences, get_daily_history
    from detection.temporal_model import predict_temporal_risk
    from detection.risk_score import temporal_risk_adjustment

    daily_history = get_daily_history(settings.db_path, account)
    history_days = len(daily_history)

    if history_days >= 7:
        seqs = build_score_sequences(settings.db_path, account)
        if len(seqs) > 0:
            temporal_prob = predict_temporal_risk(temporal_model, seqs[-1])
            score.score = temporal_risk_adjustment(
                snapshot_score=score.score,
                temporal_score=temporal_prob,
                history_days=history_days,
                temporal_weight=settings.temporal_weight,
            )


def run(
    asset_pairs: list[tuple[str | None, str | None]] | None = None,
    multi_pair: bool = False,
    no_submit: bool = False,
    use_uncertainty: bool = True,
) -> list[RiskScore]:
    """Run one scoring pass over the given asset pairs and return the resulting scores.

    `asset_pairs` is a list of `(base_asset, counter_asset)` tuples in
    `CODE:ISSUER` form (None for native XLM). Defaults to a single
    XLM/USDC pair for local testing.

    When `multi_pair=True`, trades for all pairs are loaded upfront and
    cross-asset correlation analysis is performed once across all pairs.
    The resulting cross-pair features are included in each account's
    feature vector.

    When ``use_uncertainty=True`` (default), loads calibration artifacts
    and includes conformal prediction intervals in the returned scores.
    Falls back silently if no calibration artifacts are found.
    """
    asset_pairs = asset_pairs or [
        (None, "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")
    ]
    models = load_models()
    calibrators = load_calibration() if use_uncertainty else {}
    scores: list[RiskScore] = []
    all_rings: list[dict] = []
    scored_features: list[dict] = []
    scored_wallets: list[str] = []
    scored_pairs: list[str] = []

    # Pre-load all trades when running in multi-pair mode
    trades_by_pair: dict[str, pd.DataFrame] = {}
    correlated_pairs: list[tuple[str, str, float]] = []
    cross_pair_wallets_map: dict[str, list[str]] = {}
    all_rings: list[dict] = []

    if multi_pair:
        for base_asset, counter_asset in asset_pairs:
            pair_key = f"{base_asset or 'XLM'}/{counter_asset or 'XLM'}"
            trades = load_historical_trades(base_asset=base_asset, counter_asset=counter_asset)
            if not trades.empty:
                trades_by_pair[pair_key] = trades

        if trades_by_pair:
            volume_matrix = build_volume_time_series(trades_by_pair)
            correlated_pairs = find_correlated_pairs(volume_matrix)
            cross_pair_wallets_map = find_cross_pair_wallets(trades_by_pair, correlated_pairs)

            shared_counts: dict[tuple[str, str], int] = {}
            for pa, pb, _ in correlated_pairs:
                count = sum(
                    1 for w_pairs in cross_pair_wallets_map.values()
                    if pa in w_pairs and pb in w_pairs
                )
                shared_counts[(pa, pb)] = count
            save_pair_correlations(correlated_pairs, "spearman", shared_counts)
            logger.info("Found %d correlated pair combinations", len(correlated_pairs))

    for base_asset, counter_asset in asset_pairs:
        pair_key = f"{base_asset or 'XLM'}/{counter_asset or 'XLM'}"

        if multi_pair:
            trades = trades_by_pair.get(pair_key, pd.DataFrame())
        else:
            trades = load_historical_trades(base_asset=base_asset, counter_asset=counter_asset)

        if trades.empty:
            logger.info("No trades found for %s/%s", base_asset, counter_asset)
            continue

        as_of = pd.Timestamp(trades["ledger_close_time"].max())
        graph = build_transaction_graph(trades)
        rings = find_wash_rings(graph)
        all_rings.extend(rings)
        _ring_membership = build_ring_membership_index(rings, trades=trades)
        accounts = pd.unique(trades[["base_account", "counter_account"]].values.ravel())
        accounts = accounts[pd.notna(accounts)]  # drop None (pool trades have no counterparty wallet)
        account_metadata = load_account_metadata(list(accounts))
        since = as_of.to_pydatetime() - timedelta(days=settings.trade_history_lookback_days)
        all_order_book_events = load_order_book_events_for_pair(
            base_asset,
            counter_asset,
            since=since,
        )
        order_book_events = pd.DataFrame([e.model_dump() for e in all_order_book_events])

        if "trade_type" in trades.columns:
            pool_trades = trades.loc[trades["trade_type"] == TradeType.LIQUIDITY_POOL]
            save_liquidity_pool_trades(pool_trades)

        path_payments = load_path_payments_for_accounts(list(accounts), since)
        save_path_payments(path_payments)
        circular_routes = detect_atomic_circular_routes(path_payments)
        save_circular_routes(circular_routes)
        path_cycles = detect_cycles_from_payments(path_payments, root_accounts=set(accounts))
        save_path_payment_cycles(path_cycles)
        save_alerts(path_payment_cycles_to_alerts(path_cycles))

        for account in accounts:
            features = build_feature_vector(
                trades,
                account,
                as_of,
                order_book_events=order_book_events,
                account_metadata=account_metadata,
                trades_by_pair=trades_by_pair if multi_pair else None,
                correlated_pairs=correlated_pairs if multi_pair else None,
                cross_pair_wallets=cross_pair_wallets_map if multi_pair else None,
                path_payments=path_payments,
                path_cycles=path_cycles,
                ring_membership=_ring_membership,
            )
            if calibrators:
                uncertainty = score_with_uncertainty(models, features, calibrators=calibrators)
                probability = uncertainty["score"] / 100.0
                _, confidence = score_feature_vector(models, features)
                score = RiskScore.combine(
                    wallet=account,
                    asset_pair=pair_key,
                    benford_mad=features.get("benford_mad_24h", 0.0),
                    benford_mad_threshold=settings.benford_mad_threshold,
                    ml_probability=probability,
                    ml_confidence=confidence,
                    score_lower=uncertainty["score_lower"],
                    score_upper=uncertainty["score_upper"],
                    prediction_set=uncertainty.get("prediction_set"),
                    coverage_guarantee=uncertainty.get("coverage_guarantee"),
                    sandwich_signal=features.get("pdc_5m", 0.0),
                    sandwich_weight=settings.pdc_discount_weight,
                )
            else:
                probability, confidence = score_feature_vector(models, features)
                score = RiskScore.combine(
                    wallet=account,
                    asset_pair=pair_key,
                    benford_mad=features.get("benford_mad_24h", 0.0),
                    benford_mad_threshold=settings.benford_mad_threshold,
                    ml_probability=probability,
                    ml_confidence=confidence,
                    sandwich_signal=features.get("pdc_5m", 0.0),
                    sandwich_weight=settings.pdc_discount_weight,
                )
            adjust_score_with_temporal(account, pair_key, score, models)
            scores.append(score)
            scored_features.append(features)
            scored_wallets.append(account)
            scored_pairs.append(pair_key)

    logger.info("Computed %d risk scores", len(scores))

    # Record scored features for drift detection
    if scored_features:
        try:
            record_scored_features(scored_features, scored_wallets, scored_pairs)
        except Exception:
            logger.exception("Failed to record scored features for drift detection")

    save_scores(scores)
    save_rings(all_rings)

    # Persist feature vectors and compute+cache SHAP values using XGBoost model.
    if scored_features:
        feature_vec_rows = [
            {"wallet": w, "asset_pair": p, "features": f}
            for w, p, f in zip(scored_wallets, scored_pairs, scored_features)
        ]
        save_feature_vectors(feature_vec_rows)
        xgb_model = models.get("xgboost")
        if xgb_model is not None:
            from detection.storage import save_shap_values

            for row in feature_vec_rows:
                try:
                    explanation = explain_score(xgb_model, row["features"])
                    top = top_contributing_features(explanation, n=5)
                    shap_payload = [{"feature": f, "shap_value": v} for f, v in top]
                    save_shap_values(row["wallet"], row["asset_pair"], shap_payload)
                except Exception:
                    logger.exception(
                        "Failed to compute SHAP for wallet=%s pair=%s",
                        row["wallet"],
                        row["asset_pair"],
                    )

    _enqueue_webhook_alerts(scores)

    _submit_on_chain(scores, no_submit=no_submit)

    return scores


def _enqueue_webhook_alerts(scores: list[RiskScore]) -> None:
    try:
        from detection.webhook_queue import enqueue, init_db as init_q
        from detection.webhook_registry import get_matching_subscribers, init_db as init_r

        init_r()
        init_q()
        for score in scores:
            payload = score.model_dump()
            payload["score_lower"] = score.score_lower
            payload["score_upper"] = score.score_upper
            for sub in get_matching_subscribers(score):
                enqueue(sub.subscriber_id, payload)
    except Exception:
        logger.exception("Failed to enqueue webhook alerts")


def _submit_on_chain(scores: list[RiskScore], no_submit: bool = False) -> None:
    """Submit high-risk scores to the Soroban contract."""
    if no_submit:
        logger.info("On-chain submission skipped via --no-submit")
        return
    if not settings.score_contract_id or not settings.service_secret_key:
        return

    try:
        from detection.soroban_publisher import SorobanPublisher

        publisher = SorobanPublisher(
            contract_id=settings.score_contract_id,
            secret_key=settings.service_secret_key,
            soroban_rpc_url=settings.soroban_rpc_url,
            network_passphrase=settings.network_passphrase,
            circuit_breaker_threshold=settings.soroban_circuit_breaker_threshold,
            circuit_reset_seconds=settings.soroban_circuit_reset_seconds,
        )
        high_risk = [s for s in scores if s.score >= settings.risk_score_threshold]
        if high_risk:
            results = publisher.submit_batch(high_risk)
            success_count = sum(
                1 for v in results.values()
                if isinstance(v, str) and v != "skipped" and not v.startswith("ERROR: ")
            )
            logger.info("Submitted %d scores on-chain", success_count)
    except Exception:
        logger.exception("Failed to submit scores on-chain")


async def async_run(
    asset_pairs: list[tuple[str | None, str | None]] | None = None,
    max_concurrency: int = 20,
    use_uncertainty: bool = True,
) -> list[RiskScore]:
    """Async version of `run()` using concurrent I/O and batched ML inference.

    Fetches all account metadata concurrently (bounded by `max_concurrency`)
    and scores all accounts in a single batched `predict_proba` call per model.
    Produces identical scores to synchronous `run()` for the same input data.

    When ``use_uncertainty=True`` (default), loads calibration artifacts
    and includes conformal prediction intervals in the returned scores.
    Falls back silently if no calibration artifacts are found.
    """
    asset_pairs = asset_pairs or [
        (None, "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")
    ]
    models = load_models()
    calibrators = load_calibration() if use_uncertainty else {}
    scores: list[RiskScore] = []
    all_rings: list[dict] = []

    scored_features: list[dict] = []
    scored_wallets: list[str] = []
    scored_pairs: list[str] = []

    async with AsyncHorizonClient(settings.horizon_url, max_concurrency=max_concurrency) as client:
        for base_asset, counter_asset in asset_pairs:
            pair_key = f"{base_asset or 'XLM'}/{counter_asset or 'XLM'}"

            trades = await async_load_historical_trades(
                base_asset=base_asset, counter_asset=counter_asset, client=client
            )

            if trades.empty:
                logger.info("No trades found for %s/%s", base_asset, counter_asset)
                continue

            as_of = pd.Timestamp(trades["ledger_close_time"].max())
            accounts = pd.unique(trades[["base_account", "counter_account"]].values.ravel())
            accounts = list(accounts[pd.notna(accounts)])  # drop None (pool trades have no counterparty wallet)

            since = as_of.to_pydatetime() - timedelta(days=settings.trade_history_lookback_days)
            account_metadata, all_order_book_events = await asyncio.gather(
                async_load_account_metadata(accounts, client),
                async_load_order_book_events_for_pair(base_asset, counter_asset, since, client),
            )

            order_book_events = pd.DataFrame([e.model_dump() for e in all_order_book_events])

            if "trade_type" in trades.columns:
                pool_trades = trades.loc[trades["trade_type"] == TradeType.LIQUIDITY_POOL]
                save_liquidity_pool_trades(pool_trades)

            path_payments_per_account = await asyncio.gather(
                *(async_load_path_payments(account, since, client) for account in accounts)
            )
            path_payments = [p for payments in path_payments_per_account for p in payments]
            save_path_payments(path_payments)
            circular_routes = detect_atomic_circular_routes(path_payments)
            save_circular_routes(circular_routes)
            path_cycles = detect_cycles_from_payments(path_payments, root_accounts=set(accounts))
            save_path_payment_cycles(path_cycles)
            save_alerts(path_payment_cycles_to_alerts(path_cycles))

            feature_vectors = [
                build_feature_vector(
                    trades,
                    account,
                    as_of,
                    order_book_events=order_book_events,
                    account_metadata=account_metadata,
                    path_payments=path_payments,
                    path_cycles=path_cycles,
                )
                for account in accounts
            ]

            batch_results = score_feature_matrix(models, feature_vectors)

            for account, features, (probability, confidence) in zip(
                accounts, feature_vectors, batch_results
            ):
                if calibrators:
                    uncertainty = score_with_uncertainty(models, features, calibrators=calibrators)
                    prob = uncertainty["score"] / 100.0
                    score = RiskScore.combine(
                        wallet=account,
                        asset_pair=pair_key,
                        benford_mad=features.get("benford_mad_24h", 0.0),
                        benford_mad_threshold=settings.benford_mad_threshold,
                        ml_probability=prob,
                        ml_confidence=confidence,
                        score_lower=uncertainty["score_lower"],
                        score_upper=uncertainty["score_upper"],
                        prediction_set=uncertainty.get("prediction_set"),
                        coverage_guarantee=uncertainty.get("coverage_guarantee"),
                        sandwich_signal=features.get("pdc_5m", 0.0),
                        sandwich_weight=settings.pdc_discount_weight,
                    )
                else:
                    score = RiskScore.combine(
                        wallet=account,
                        asset_pair=pair_key,
                        benford_mad=features.get("benford_mad_24h", 0.0),
                        benford_mad_threshold=settings.benford_mad_threshold,
                        ml_probability=probability,
                        ml_confidence=confidence,
                        sandwich_signal=features.get("pdc_5m", 0.0),
                        sandwich_weight=settings.pdc_discount_weight,
                    )
                adjust_score_with_temporal(account, pair_key, score, models)
                scores.append(score)
                scored_features.append(features)
                scored_wallets.append(account)
                scored_pairs.append(pair_key)

    logger.info("Computed %d risk scores", len(scores))

    # Record scored features for drift detection
    if scored_features:
        try:
            record_scored_features(scored_features, scored_wallets, scored_pairs)
        except Exception:
            logger.exception("Failed to record scored features for drift detection")

    save_scores(scores)
    save_rings(all_rings)
    _enqueue_webhook_alerts(scores)
    _submit_on_chain(scores)

    return scores


def run_streaming(
    asset_pair: tuple[str | None, str | None] | None = None,
    batch_size: int = 500,
    flush_interval_seconds: float = 30.0,
    _now: Callable[[], float] = time.monotonic,
) -> None:
    """Stream trades from Horizon and score in rolling batches.

    Parameters
    ----------
    asset_pair:
        ``(base_asset, counter_asset)`` tuple in ``CODE:ISSUER`` form
        (``None`` for native XLM). Defaults to XLM/USDC.
    batch_size:
        Number of trades to accumulate before triggering a flush.
    flush_interval_seconds:
        Maximum seconds to wait before flushing a partial batch.
    _now:
        Injectable time source (defaults to ``time.monotonic``). Test code
        can monkey-patch this to advance time without sleeping.
    """
    asset_pair = asset_pair or (
        None,
        "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
    )
    models = load_models()
    calibrators = load_calibration()
    if calibrators:
        logger.info("Loaded calibration artifacts for %d models", len(calibrators))
    pair_key = f"{asset_pair[0] or 'XLM'}/{asset_pair[1] or 'XLM'}"

    # Read cursor from file so we resume where we left off.
    cursor = "now"
    cursor_path = Path(settings.cursor_path)
    if cursor_path.exists():
        cursor = cursor_path.read_text().strip()
        logger.info("Resuming stream from cursor %s", cursor)

    buffer: list[Trade] = []
    last_flush_time = _now()
    last_cursor: str = cursor

    try:
        for trade, event_cursor in stream_trades_with_cursor(cursor=cursor):
            buffer.append(trade)
            if event_cursor:
                last_cursor = event_cursor

            now = _now()
            if len(buffer) >= batch_size or (now - last_flush_time) >= flush_interval_seconds:
                _flush_streaming_buffer(buffer, models, pair_key, asset_pair, last_cursor, calibrators)
                buffer.clear()
                last_flush_time = now
    except KeyboardInterrupt:
        logger.info("Stream interrupted, flushing remaining %d trades", len(buffer))
        if buffer:
            _flush_streaming_buffer(buffer, models, pair_key, asset_pair, last_cursor, calibrators)
        raise


def _flush_streaming_buffer(
    buffer: list[Trade],
    models: dict,
    pair_key: str,
    asset_pair: tuple[str | None, str | None],
    cursor: str,
    calibrators: dict | None = None,
) -> None:
    """Score all accounts in *buffer* and persist results + cursor."""
    if not buffer:
        return

    t0 = time.monotonic()
    trades_df = pd.DataFrame([t.model_dump() for t in buffer])

    as_of = pd.Timestamp(trades_df["ledger_close_time"].max())
    accounts = pd.unique(trades_df[["base_account", "counter_account"]].values.ravel())
    accounts = [a for a in accounts if pd.notna(a) and a]
    account_metadata = load_account_metadata(accounts)

    scores: list[RiskScore] = []
    scored_features: list[dict] = []
    scored_wallets: list[str] = []
    scored_pairs: list[str] = []

    for account in accounts:
        features = build_feature_vector(
            trades_df,
            account,
            as_of,
            account_metadata=account_metadata,
        )

        if calibrators:
            uncertainty = score_with_uncertainty(models, features, calibrators=calibrators)
            probability = uncertainty["score"] / 100.0
            _, confidence = score_feature_vector(models, features)
            score = RiskScore.combine(
                wallet=account,
                asset_pair=pair_key,
                benford_mad=features.get("benford_mad_24h", 0.0),
                benford_mad_threshold=settings.benford_mad_threshold,
                ml_probability=probability,
                ml_confidence=confidence,
                score_lower=uncertainty["score_lower"],
                score_upper=uncertainty["score_upper"],
                prediction_set=uncertainty.get("prediction_set"),
                coverage_guarantee=uncertainty.get("coverage_guarantee"),
                sandwich_signal=features.get("pdc_5m", 0.0),
                sandwich_weight=settings.pdc_discount_weight,
            )
        else:
            probability, confidence = score_feature_vector(models, features)
            score = RiskScore.combine(
                wallet=account,
                asset_pair=pair_key,
                benford_mad=features.get("benford_mad_24h", 0.0),
                benford_mad_threshold=settings.benford_mad_threshold,
                ml_probability=probability,
                ml_confidence=confidence,
                sandwich_signal=features.get("pdc_5m", 0.0),
                sandwich_weight=settings.pdc_discount_weight,
            )
        adjust_score_with_temporal(account, pair_key, score, models)
        scores.append(score)
        scored_features.append(features)
        scored_wallets.append(account)
        scored_pairs.append(pair_key)

    elapsed = time.monotonic() - t0

    if scored_features:
        try:
            record_scored_features(scored_features, scored_wallets, scored_pairs)
        except Exception:
            logger.exception("Failed to record scored features in streaming flush")

    save_scores(scores)

    # Persist cursor for resumption after restart.
    cursor_path = Path(settings.cursor_path)
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(cursor)

    logger.info(
        "Flush: %d trades, %d accounts scored, %.2fs elapsed",
        len(buffer),
        len(scores),
        elapsed,
    )


if __name__ == "__main__":
    run()
