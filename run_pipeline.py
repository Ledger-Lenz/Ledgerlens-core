"""LedgerLens detection pipeline entry point.

Loads recent trades, computes Benford + ML features per wallet/asset pair,
scores each with the trained ensemble, and publishes the resulting
`RiskScore` records to ledgerlens-api (and optionally ledgerlens-contracts).
See README.md's "LedgerLens Organization" section for how this fits with
the other repos in the org.
"""

import logging

import pandas as pd

from config.settings import settings
from detection.feature_engineering import build_feature_vector
from detection.model_inference import load_models, score_feature_vector
from detection.risk_score import RiskScore
from detection.storage import save_scores
from ingestion.account_loader import load_account_metadata
from ingestion.historical_loader import load_historical_trades
from ingestion.operations_loader import load_order_book_events

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ledgerlens.pipeline")


def run(asset_pairs: list[tuple[str | None, str | None]] | None = None) -> list[RiskScore]:
    """Run one scoring pass over the given asset pairs and return the resulting scores.

    `asset_pairs` is a list of `(base_asset, counter_asset)` tuples in
    `CODE:ISSUER` form (None for native XLM). Defaults to a single
    XLM/USDC pair for local testing.
    """
    asset_pairs = asset_pairs or [(None, "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")]
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

        for account in accounts:
            order_book_events = pd.DataFrame(
                [e.model_dump() for e in load_order_book_events(account)]
            )
            features = build_feature_vector(
                trades, account, as_of, order_book_events=order_book_events, account_metadata=account_metadata
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


if __name__ == "__main__":
    run()
