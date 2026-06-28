"""Robustness evaluation framework for the LedgerLens ensemble.

Measures model performance degradation under each evasion strategy by
generating adversarial datasets and scoring them with pre-trained models.
"""

import json
import math
import numpy as np
from pydantic import BaseModel
from scipy.stats import norm
from sklearn.metrics import f1_score, roc_auc_score

from detection.dataset import build_training_dataset
from ingestion.adversarial_data import ALL_STRATEGIES, generate_adversarial_dataset
from ingestion.synthetic_data import generate_synthetic_dataset
from detection.adversarial_attack import FEATURE_CONSTRAINTS, pgd_attack

from detection.feature_engineering import FEATURE_NAMES


class RobustnessReport(BaseModel):
    model_version: str
    asr: dict
    mean_map: float
    p95_map: float
    certified_radius: float
    n_samples: int
    epsilon: float




def _score_models(models: dict, df) -> dict[str, float]:
    """Return mean AUC-ROC and F1 across all models in ``models``."""
    from detection.feature_engineering import FEATURE_NAMES

    X = df[FEATURE_NAMES]
    y = df["label"]
    if y.nunique() < 2:
        return {"auc_roc": float("nan"), "f1": float("nan")}

    auc_rocs, f1s = [], []
    for name, model in models.items():
        if name == "temporal_lstm":
            continue
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


def _ensemble_probas(models: dict, df) -> np.ndarray:
    """Return mean ensemble probability (positive class) for each row in df."""
    X = df[FEATURE_NAMES].fillna(0.0)
    probs = []
    for name, m in models.items():
        if name == "temporal_lstm":
            continue
        probs.append(m.predict_proba(X)[:, 1])
    return np.mean(np.vstack(probs), axis=0)


def compute_robustness_report(models: dict, df, n_samples: int = 200, epsilon: float = 0.1, steps: int = 10, seed: int = 42) -> RobustnessReport:
    """Compute ASR, MAP, and certified radius on the provided dataset.

    - ASR computed at epsilons 0.05, 0.10, 0.20 (fraction of true positives flipped)
    - MAP computed per true positive by binary-searching epsilon where PGD succeeds
    - Certified radius estimated via randomized smoothing (Monte Carlo)
    """
    from detection.storage import save_robustness_report

    rng = np.random.RandomState(seed)
    probs = _ensemble_probas(models, df)
    labels = df["label"].values
    # True positives as those labeled 1 and predicted positive at 0.5
    tp_mask = (labels == 1) & (probs >= 0.5)
    tp_indices = np.where(tp_mask)[0]

    asr_eps = [0.05, 0.10, 0.20]
    asr: dict = {}

    # small helper to get feature vector as dict for a row
    def row_to_vec(i):
        return {f: float(df.iloc[i][f]) for f in FEATURE_NAMES}

    for e in asr_eps:
        flipped = 0
        for i in tp_indices:
            vec = row_to_vec(i)
            _, p = pgd_attack(vec, models, epsilon=e, alpha=e / max(1, steps), steps=steps)
            if p < 0.5:
                flipped += 1
        asr["{:.2f}".format(e)] = float(flipped / len(tp_indices)) if len(tp_indices) else 0.0

    # MAP: minimal epsilon per TP via binary search on [0, max_eps]
    maps = []
    max_eps = 1.0
    for i in tp_indices:
        lo = 0.0
        hi = max_eps
        found = False
        for _ in range(10):
            mid = (lo + hi) / 2.0
            vec = row_to_vec(i)
            _, p = pgd_attack(vec, models, epsilon=mid, alpha=mid / max(1, steps), steps=steps)
            if p < 0.5:
                found = True
                hi = mid
            else:
                lo = mid
        if found:
            maps.append(hi)
    mean_map = float(np.mean(maps)) if maps else 0.0
    p95_map = float(np.percentile(maps, 95)) if maps else 0.0

    # Certified radius (Monte Carlo smoothing)
    sigma = 0.25
    certs = []
    for i in tp_indices:
        vec = row_to_vec(i)
        noisy_list = []
        for _ in range(n_samples):
            noisy = {f: float(vec[f] + rng.normal(scale=sigma)) for f in FEATURE_NAMES}
            # enforce bounds
            for f in FEATURE_NAMES:
                c = FEATURE_CONSTRAINTS.get(f, {})
                noisy[f] = max(c.get("min", -math.inf), noisy[f])
                noisy[f] = min(c.get("max", math.inf), noisy[f])
            noisy_list.append(noisy)
        import pandas as pd
        noisy_df = pd.DataFrame(noisy_list)[FEATURE_NAMES].fillna(0.0)
        probs_noisy = _ensemble_probas(models, noisy_df)
        count_pos = int(np.sum(probs_noisy >= 0.5))
        p_hat = count_pos / n_samples
        if p_hat <= 0.5:
            certs.append(0.0)
        else:
            # approximate lower bound (probabilistic)
            try:
                r = sigma * norm.ppf(p_hat)
                certs.append(max(0.0, float(r)))
            except Exception:
                certs.append(0.0)
    certified_radius = float(np.mean(certs)) if certs else 0.0

    # Model version: read metadata file if exists
    import os
    from config.settings import settings
    meta_path = os.path.join(settings.model_dir, "training_metadata.json")
    model_version = "unknown"
    try:
        with open(meta_path, "r") as f:
            j = json.load(f)
            model_version = j.get("version", "unknown")
    except Exception:
        model_version = "unknown"

    report = RobustnessReport(
        model_version=model_version,
        asr=asr,
        mean_map=mean_map,
        p95_map=p95_map,
        certified_radius=certified_radius,
        n_samples=n_samples,
        epsilon=epsilon,
    )

    # persist
    try:
        from detection.storage import save_robustness_report
        save_robustness_report(report.model_dump())
    except Exception:
        # best-effort persist; failures should not crash reporting
        pass

    return report


# ---------------------------------------------------------------------------
# Live red team metrics
#
# These read the evasion-event log written by the continuous red team loop
# (detection.red_team) and summarise how the model is currently holding up
# against the adversarial attacker. Exposed via GET /api/v1/model/robustness.
# ---------------------------------------------------------------------------


def evasion_rate_24h(db_path: str | None = None) -> float:
    """Fraction of seed attacks in the last 24h that successfully evaded.

    Returns ``0.0`` when no attacks were logged in the window.
    """
    from datetime import datetime, timedelta, timezone

    from detection.red_team.evasion_logger import get_evasion_events

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    events = get_evasion_events(since=since, db_path=db_path)
    if not events:
        return 0.0
    evaded = sum(1 for e in events if e["is_evasion"])
    return float(evaded / len(events))


def mean_generations_to_evade(db_path: str | None = None) -> float:
    """Average GA generations needed across successful evasions (``0.0`` if none)."""
    from detection.red_team.evasion_logger import get_evasion_events

    events = [e for e in get_evasion_events(only_evasions=True, db_path=db_path)]
    if not events:
        return 0.0
    return float(np.mean([e["attacker_generation"] for e in events]))


def _evasion_rate(events: list[dict]) -> float:
    if not events:
        return 0.0
    return float(sum(1 for e in events if e["is_evasion"]) / len(events))


def hardening_delta(db_path: str | None = None) -> float:
    """Change in evasion rate from before to after the most recent retrain.

    Positive means evasions became *more* frequent after retraining (a
    regression); negative means hardening reduced the evasion rate. Returns
    ``0.0`` when there is no recorded retrain to split the log on.
    """
    from detection.red_team.evasion_logger import get_evasion_events
    from detection.storage import get_retrain_runs

    runs = get_retrain_runs(limit=1, db_path=db_path)
    if not runs:
        return 0.0
    retrain_ts = runs[0]["triggered_at"]

    events = get_evasion_events(db_path=db_path)
    before = [e for e in events if e["created_at"] < retrain_ts]
    after = [e for e in events if e["created_at"] >= retrain_ts]
    if not before or not after:
        return 0.0
    return _evasion_rate(after) - _evasion_rate(before)


def live_robustness_metrics(db_path: str | None = None) -> dict:
    """Aggregate the live red team metrics surfaced by the robustness endpoint."""
    return {
        "evasion_rate_24h": evasion_rate_24h(db_path=db_path),
        "mean_generations_to_evade": mean_generations_to_evade(db_path=db_path),
        "hardening_delta": hardening_delta(db_path=db_path),
    }
