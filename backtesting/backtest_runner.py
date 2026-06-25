"""Backtesting framework for evaluating LedgerLens models against labelled historical data.

Loads a labelled CSV dataset (wallet, label, start_date, end_date),
runs the feature extraction and scoring pipeline over the specified date range
for each wallet, and computes precision/recall/F1/AUC-ROC/average precision
at configurable score thresholds.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("ledgerlens.backtest")


@dataclass
class WalletResult:
    wallet: str
    label: int
    score: float
    probability: float
    confidence: float


@dataclass
class BacktestReport:
    dataset_path: str
    threshold: int
    total_wallets: int
    labelled_positive: int
    labelled_negative: int
    predicted_positive: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1: float
    auc_roc: float
    average_precision: float
    per_wallet: list[dict]
    generated_at: str
    thresholds_sweep: list[dict] = field(default_factory=list)


def load_labelled_dataset(csv_path: str) -> pd.DataFrame:
    """Load a labelled CSV with columns: wallet, label, start_date, end_date.

    label: 1 = confirmed wash trader, 0 = clean.
    start_date/end_date define the observation window for each wallet.
    """
    df = pd.read_csv(csv_path)
    required = {"wallet", "label"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    df["label"] = df["label"].astype(int)
    return df


def _compute_metrics(
    y_true: np.ndarray,
    y_scores: np.ndarray,
    threshold: int,
) -> dict:
    """Compute classification metrics at a given score threshold."""
    from sklearn.metrics import (
        average_precision_score,
        roc_auc_score,
    )

    y_pred = (y_scores >= threshold).astype(int)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    try:
        auc = float(roc_auc_score(y_true, y_scores))
    except ValueError:
        auc = 0.0

    try:
        ap = float(average_precision_score(y_true, y_scores))
    except ValueError:
        ap = 0.0

    return {
        "threshold": threshold,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "auc_roc": auc,
        "average_precision": ap,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "predicted_positive": tp + fp,
    }


def run_backtest(
    dataset_path: str,
    threshold: int = 70,
    model_dir: str | None = None,
    sweep_thresholds: list[int] | None = None,
) -> BacktestReport:
    """Run the full backtest pipeline.

    Loads the labelled dataset, runs feature extraction and model scoring
    for each wallet, then computes aggregate metrics.

    Args:
        dataset_path: Path to the labelled CSV.
        threshold: Primary score threshold for classification (0-100).
        model_dir: Directory containing trained model artifacts.
        sweep_thresholds: Optional list of additional thresholds to evaluate.
    """
    from config.settings import settings
    from detection.feature_engineering import FEATURE_NAMES
    from detection.model_inference import load_models, score_feature_vector

    model_dir = model_dir or settings.model_dir

    df = load_labelled_dataset(dataset_path)
    logger.info("Loaded %d wallets from %s", len(df), dataset_path)

    try:
        models = load_models(model_dir)
    except FileNotFoundError:
        logger.error("No trained models found in %s — run `cli.py train` first", model_dir)
        raise

    wallet_results: list[WalletResult] = []

    for _, row in df.iterrows():
        wallet = row["wallet"]
        label = int(row["label"])

        features = _extract_features_for_wallet(wallet, row, df)

        for fname in FEATURE_NAMES:
            features.setdefault(fname, 0.0)

        try:
            probability, confidence = score_feature_vector(models, features)
        except Exception as exc:
            logger.warning("Scoring failed for %s: %s", wallet, exc)
            probability, confidence = 0.0, 0.0

        score = int(probability * 100)

        wallet_results.append(WalletResult(
            wallet=wallet,
            label=label,
            score=score,
            probability=probability,
            confidence=confidence,
        ))

    y_true = np.array([w.label for w in wallet_results])
    y_scores = np.array([w.score for w in wallet_results])

    primary = _compute_metrics(y_true, y_scores, threshold)

    thresholds_sweep = []
    for t in (sweep_thresholds or [50, 60, 70, 80, 90]):
        thresholds_sweep.append(_compute_metrics(y_true, y_scores, t))

    per_wallet = [
        {
            "wallet": w.wallet,
            "label": w.label,
            "score": w.score,
            "probability": round(w.probability, 4),
            "confidence": round(w.confidence, 4),
            "correct": (w.score >= threshold) == (w.label == 1),
        }
        for w in wallet_results
    ]

    return BacktestReport(
        dataset_path=dataset_path,
        threshold=threshold,
        total_wallets=len(wallet_results),
        labelled_positive=int(y_true.sum()),
        labelled_negative=int((1 - y_true).sum()),
        predicted_positive=primary["predicted_positive"],
        true_positives=primary["tp"],
        false_positives=primary["fp"],
        false_negatives=primary["fn"],
        true_negatives=primary["tn"],
        precision=primary["precision"],
        recall=primary["recall"],
        f1=primary["f1"],
        auc_roc=primary["auc_roc"],
        average_precision=primary["average_precision"],
        per_wallet=per_wallet,
        generated_at=datetime.utcnow().isoformat(),
        thresholds_sweep=thresholds_sweep,
    )


def _extract_features_for_wallet(
    wallet: str,
    row: pd.Series,
    dataset: pd.DataFrame,
) -> dict[str, float]:
    """Extract features for a single wallet.

    In a full deployment, this would load historical trades for the wallet
    within [start_date, end_date] and run the feature engineering pipeline.
    For synthetic/CI backtest datasets, features may be embedded in the CSV
    columns directly.
    """
    from detection.feature_engineering import FEATURE_NAMES

    features: dict[str, float] = {}
    for fname in FEATURE_NAMES:
        if fname in row.index:
            try:
                features[fname] = float(row[fname])
            except (ValueError, TypeError):
                features[fname] = 0.0

    return features


def save_report(report: BacktestReport, output_dir: str = ".") -> str:
    """Save the backtest report as JSON. Returns the output path."""
    import dataclasses

    output_path = Path(output_dir) / f"backtest_results_{datetime.utcnow().strftime('%Y-%m-%d')}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report_dict = dataclasses.asdict(report)
    with open(output_path, "w") as f:
        json.dump(report_dict, f, indent=2, default=str)

    logger.info("Backtest report saved to %s", output_path)
    return str(output_path)
