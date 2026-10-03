"""Backtesting framework for evaluating LedgerLens models against labelled historical data.

Loads a labelled CSV dataset (wallet, label, start_date, end_date),
runs the feature extraction and scoring pipeline over the specified date range
for each wallet, and computes precision/recall/F1/AUC-ROC/average precision
at configurable score thresholds.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        raise ValueError(
            f"Labelled dataset {csv_path!r} is missing required column(s): "
            f"{sorted(missing)}. Expected columns: wallet, label "
            f"(start_date and end_date are optional)."
        )
    try:
        df["label"] = df["label"].astype(int)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Column 'label' in {csv_path!r} must contain only integers (0 or 1); "
            f"found a non-numeric or missing value: {exc}"
        ) from exc
    invalid_labels = set(df["label"].unique()) - {0, 1}
    if invalid_labels:
        raise ValueError(
            f"Column 'label' in {csv_path!r} must contain only 0 (clean) or 1 "
            f"(confirmed wash trader); found invalid value(s): {sorted(invalid_labels)}"
        )
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


# ---------------------------------------------------------------------------
# Walk-forward evaluation
#
# Train strictly on data observed up to T, evaluate strictly after T, then
# roll T forward by `step`.  This is the recommended default for model
# evaluation: a random or whole-history split lets information from the
# evaluation period leak into training (lookahead bias) and inflates metrics
# relative to real deployment.
# ---------------------------------------------------------------------------


class LookaheadBiasError(ValueError):
    """Raised when training data for a fold overlaps or postdates its test window."""


@dataclass
class WalkForwardConfig:
    """Window/step configuration for :func:`run_walk_forward_backtest`.

    Attributes:
        train_window: Length of each training window (rolling). ``None`` uses
            an expanding window over all history before the test window.
        test_window: Length of each evaluation window.
        step: How far the train/test boundary moves between folds.
        time_column: Column holding the time the features were observed.
        label_time_column: Optional column holding when the label became
            known (e.g. case-closure date).  Rows whose label is not yet known
            at the fold boundary are excluded from training.
        gap: Optional embargo between the end of training and the start of
            testing, for features computed over trailing windows.
    """

    train_window: pd.Timedelta | None
    test_window: pd.Timedelta
    step: pd.Timedelta
    time_column: str = "timestamp"
    label_time_column: str | None = None
    gap: pd.Timedelta = field(default_factory=lambda: pd.Timedelta(0))

    def __post_init__(self) -> None:
        for name in ("test_window", "step"):
            if pd.Timedelta(getattr(self, name)) <= pd.Timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.train_window is not None and pd.Timedelta(self.train_window) <= pd.Timedelta(0):
            raise ValueError("train_window must be positive or None (expanding)")
        if pd.Timedelta(self.gap) < pd.Timedelta(0):
            raise ValueError("gap must not be negative")


@dataclass
class WalkForwardFold:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    metrics: dict


@dataclass
class WalkForwardReport:
    threshold: int
    folds: list[WalkForwardFold]
    aggregate: dict
    generated_at: str


def walk_forward_splits(
    df: pd.DataFrame,
    config: WalkForwardConfig,
) -> list[tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp]]:
    """Return ``(train, test, boundary, test_end)`` for each walk-forward fold.

    Training rows satisfy ``time < boundary - gap`` (and, if configured,
    ``label_time < boundary - gap``); test rows satisfy
    ``boundary <= time < test_end``.  Every split is checked with
    :func:`assert_no_lookahead` before being returned.
    """
    times = pd.to_datetime(df[config.time_column], utc=True)
    label_times = (
        pd.to_datetime(df[config.label_time_column], utc=True) if config.label_time_column else None
    )
    first, last = times.min(), times.max()
    boundary = first + (config.train_window if config.train_window is not None else config.step)

    splits = []
    while boundary <= last:
        test_end = boundary + config.test_window
        cutoff = boundary - config.gap
        train_mask = times < cutoff
        if config.train_window is not None:
            train_mask &= times >= cutoff - config.train_window
        if label_times is not None:
            train_mask &= label_times < cutoff
        test_mask = (times >= boundary) & (times < test_end)

        train, test = df[train_mask], df[test_mask]
        if len(train) and len(test):
            assert_no_lookahead(train, test, config, boundary)
            splits.append((train, test, boundary, test_end))
        boundary += config.step
    return splits


def assert_no_lookahead(
    train: pd.DataFrame,
    test: pd.DataFrame,
    config: WalkForwardConfig,
    boundary: pd.Timestamp,
) -> None:
    """Raise :class:`LookaheadBiasError` if ``train`` sees test-window data."""
    if set(train.index) & set(test.index):
        raise LookaheadBiasError("training and test windows share rows")
    cutoff = boundary - config.gap
    train_times = pd.to_datetime(train[config.time_column], utc=True)
    if train_times.max() >= cutoff:
        raise LookaheadBiasError(
            f"training features observed at {train_times.max()} are not before the fold cutoff {cutoff}"
        )
    if config.label_time_column:
        label_times = pd.to_datetime(train[config.label_time_column], utc=True)
        if label_times.max() >= cutoff:
            raise LookaheadBiasError(
                f"training labels known at {label_times.max()} are not before the fold cutoff {cutoff}"
            )
    test_times = pd.to_datetime(test[config.time_column], utc=True)
    if test_times.min() < boundary:
        raise LookaheadBiasError("test window contains rows before the fold boundary")


def run_walk_forward_backtest(
    df: pd.DataFrame,
    config: WalkForwardConfig,
    fit: Callable[[pd.DataFrame], Any],
    predict: Callable[[Any, pd.DataFrame], np.ndarray],
    threshold: int = 70,
    label_column: str = "label",
) -> WalkForwardReport:
    """Walk-forward backtest: refit on each training window, score its test window.

    Args:
        df: Time-stamped labelled rows (features + ``label_column``).
        config: Window/step configuration (see :class:`WalkForwardConfig`).
        fit: Trains a model on a training-window frame and returns it.
        predict: Returns 0-100 risk scores for a test-window frame.
        threshold: Score threshold for classification metrics.
    """
    folds: list[WalkForwardFold] = []
    all_true: list[np.ndarray] = []
    all_scores: list[np.ndarray] = []

    for i, (train, test, boundary, test_end) in enumerate(walk_forward_splits(df, config)):
        model = fit(train)
        scores = np.asarray(predict(model, test), dtype=float)
        y_true = test[label_column].to_numpy(dtype=int)
        train_times = pd.to_datetime(train[config.time_column], utc=True)
        folds.append(WalkForwardFold(
            fold=i,
            train_start=train_times.min().isoformat(),
            train_end=train_times.max().isoformat(),
            test_start=boundary.isoformat(),
            test_end=test_end.isoformat(),
            n_train=len(train),
            n_test=len(test),
            metrics=_compute_metrics(y_true, scores, threshold),
        ))
        all_true.append(y_true)
        all_scores.append(scores)

    if not folds:
        raise ValueError("walk-forward configuration produced no folds; check window sizes against the data span")

    aggregate = _compute_metrics(np.concatenate(all_true), np.concatenate(all_scores), threshold)
    logger.info("Walk-forward backtest: %d folds, AUC-ROC %.3f", len(folds), aggregate["auc_roc"])
    return WalkForwardReport(
        threshold=threshold,
        folds=folds,
        aggregate=aggregate,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
