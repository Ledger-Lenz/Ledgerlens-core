"""Robustness evaluation framework for the LedgerLens ensemble.

Measures model performance degradation under each evasion strategy by
generating adversarial datasets and scoring them with pre-trained models.
"""

import numpy as np
from sklearn.metrics import f1_score, roc_auc_score

from detection.dataset import build_training_dataset
from ingestion.adversarial_data import ALL_STRATEGIES, generate_adversarial_dataset
from ingestion.synthetic_data import generate_synthetic_dataset


def _score_models(models: dict, df) -> dict[str, float]:
    """Return mean AUC-ROC and F1 across all models in ``models``."""
    from detection.feature_engineering import FEATURE_NAMES

    X = df[FEATURE_NAMES]
    y = df["label"]
    if y.nunique() < 2:
        return {"auc_roc": float("nan"), "f1": float("nan")}

    auc_rocs, f1s = [], []
    for model in models.values():
        y_proba = model.predict_proba(X)[:, 1]
        y_pred = model.predict(X)
        auc_rocs.append(roc_auc_score(y, y_proba))
        f1s.append(f1_score(y, y_pred))
    return {"auc_roc": float(np.mean(auc_rocs)), "f1": float(np.mean(f1s))}


def evaluate_robustness(
    models: dict,
    evasion_strategies: list[str] | None = None,
    n_trials: int = 10,
    seed: int = 42,
) -> dict:
    """For each evasion strategy, generate adversarial datasets and measure model AUC-ROC.

    Parameters
    ----------
    models:
        Dict of ``{name: fitted_classifier}`` as returned by
        ``detection.model_training.train_ensemble`` (the ``"model"`` values).
    evasion_strategies:
        Strategies to evaluate; ``None`` tests all five plus the combined case.
    n_trials:
        Number of independent datasets generated per strategy (results are averaged).

    Returns
    -------
    Dict keyed by strategy name plus ``"baseline"`` and ``"all_strategies"``, each
    containing ``auc_roc``, ``f1``, and (for non-baseline) ``delta_auc``.
    """
    strategies = evasion_strategies if evasion_strategies is not None else ALL_STRATEGIES

    results: dict = {}

    # --- Baseline (no evasion) ---
    baseline_auc, baseline_f1 = [], []
    for i in range(n_trials):
        trades, meta, events, labels = generate_synthetic_dataset(
            n_normal_accounts=50, n_wash_rings=10, ring_size=4, seed=seed + i
        )
        df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
        m = _score_models(models, df)
        if not np.isnan(m["auc_roc"]):
            baseline_auc.append(m["auc_roc"])
            baseline_f1.append(m["f1"])

    base_auc = float(np.mean(baseline_auc)) if baseline_auc else float("nan")
    base_f1 = float(np.mean(baseline_f1)) if baseline_f1 else float("nan")
    results["baseline"] = {"auc_roc": base_auc, "f1": base_f1}

    # --- Per-strategy evaluation ---
    for strategy in strategies:
        aucs, f1s = [], []
        for i in range(n_trials):
            trades, meta, events, labels = generate_adversarial_dataset(
                n_normal_accounts=50,
                n_wash_rings=10,
                ring_size=4,
                evasion_strategies=[strategy],
                seed=seed + i,
            )
            df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
            m = _score_models(models, df)
            if not np.isnan(m["auc_roc"]):
                aucs.append(m["auc_roc"])
                f1s.append(m["f1"])

        avg_auc = float(np.mean(aucs)) if aucs else float("nan")
        avg_f1 = float(np.mean(f1s)) if f1s else float("nan")
        results[strategy] = {
            "auc_roc": avg_auc,
            "f1": avg_f1,
            "delta_auc": avg_auc - base_auc,
        }

    # --- All strategies combined ---
    aucs, f1s = [], []
    for i in range(n_trials):
        trades, meta, events, labels = generate_adversarial_dataset(
            n_normal_accounts=50,
            n_wash_rings=10,
            ring_size=4,
            evasion_strategies=None,  # all
            seed=seed + i,
        )
        df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
        m = _score_models(models, df)
        if not np.isnan(m["auc_roc"]):
            aucs.append(m["auc_roc"])
            f1s.append(m["f1"])

    avg_auc = float(np.mean(aucs)) if aucs else float("nan")
    avg_f1 = float(np.mean(f1s)) if f1s else float("nan")
    results["all_strategies"] = {
        "auc_roc": avg_auc,
        "f1": avg_f1,
        "delta_auc": avg_auc - base_auc,
    }

    return results
