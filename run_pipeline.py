"""LedgerLens detection pipeline entry point.

Loads recent trades, computes Benford + ML features per wallet/asset pair,
scores each with the trained ensemble, and publishes the resulting
`RiskScore` records to ledgerlens-api (and optionally ledgerlens-contracts).
See README.md's "LedgerLens Organization" section for how this fits with
the other repos in the org.
"""

import logging
from datetime import timedelta

import pandas as pd

from config.settings import settings
from detection.feature_engineering import build_feature_vector
from detection.model_inference import load_models, score_feature_vector
from detection.risk_score import RiskScore
from detection.storage import save_scores
from ingestion.account_loader import load_account_metadata
from ingestion.historical_loader import load_historical_trades
from ingestion.operations_loader import load_order_book_events_for_pair

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ledgerlens.pipeline")


def run(
    asset_pairs: list[tuple[str | None, str | None]] | None = None,
    no_submit: bool = False,
) -> list[RiskScore]:
    """Run one scoring pass over the given asset pairs and return the resulting scores.

    `asset_pairs` is a list of `(base_asset, counter_asset)` tuples in
    `CODE:ISSUER` form (None for native XLM). Defaults to a single
    XLM/USDC pair for local testing.
    """
    asset_pairs = asset_pairs or [
        (None, "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")
    ]
    models = load_models()
    scores: list[RiskScore] = []

    for base_asset, counter_asset in asset_pairs:
        trades = load_historical_trades(base_asset=base_asset, counter_asset=counter_asset)
        if trades.empty:
            logger.info("No trades found for %s/%s", base_asset, counter_asset)
            continue

        as_of = pd.Timestamp(trades["ledger_close_time"].max())
        accounts = pd.unique(trades[["base_account", "counter_account"]].values.ravel())
        account_metadata = load_account_metadata(list(accounts))
        all_order_book_events = load_order_book_events_for_pair(
            base_asset,
            counter_asset,
            since=as_of.to_pydatetime() - timedelta(days=settings.trade_history_lookback_days),
        )
        order_book_events = pd.DataFrame([e.model_dump() for e in all_order_book_events])

        for account in accounts:
            features = build_feature_vector(
                trades,
                account,
                as_of,
                order_book_events=order_book_events,
                account_metadata=account_metadata,
            )
            probability, confidence = score_feature_vector(models, features)

            score = RiskScore.combine(
                wallet=account,
                asset_pair=f"{base_asset or 'XLM'}/{counter_asset or 'XLM'}",
                benford_mad=features.get("benford_mad_24h", 0.0),
                benford_mad_threshold=settings.benford_mad_threshold,
                ml_probability=probability,
                ml_confidence=confidence,
            )
            scores.append(score)

    logger.info("Computed %d risk scores", len(scores))
    save_scores(scores)

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
            for sub in get_matching_subscribers(score):
                enqueue(sub.subscriber_id, score.model_dump())
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


if __name__ == "__main__":
    run()
