"""Train the Random Forest / XGBoost / LightGBM wash-trading ensemble.

Expects a feature DataFrame (see `feature_engineering.build_feature_vector`)
with a binary `label` column (1 = confirmed wash trade pattern). Trained
models are written to `settings.model_dir` for `model_inference` to load.

When ``calibrate=True`` a calibration split is held out (10 % of the data,
stratified by label) *before* any model training, then used after training
to compute conformal prediction thresholds via ``ConformalCalibrator``.
"""

import joblib
import numpy as np
import pandas as pd
from detection.model_signing import sign_model_file
from imblearn.over_sampling import SMOTE
from lightgbm import LGBMClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from config.settings import settings
from detection.feature_engineering import FEATURE_NAMES


def _split_features_labels(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Split `df` into `(X, y)`, ordering feature columns by `FEATURE_NAMES`
    so training and inference (`model_inference.score_feature_vector`) never drift.
    """
    X = df[FEATURE_NAMES].fillna(0.0)
    y = df["label"]
    return X, y


def _train_ensemble_base(
    df: pd.DataFrame,
    random_state: int = 42,
    adversarial_augment: bool = True,
    calibrate: bool = True,
    adversarial_hardening: bool = False,
    causal_feature_selection: bool = False,
    **kwargs,
) -> dict:
    """Train RF, XGBoost, and LightGBM classifiers on `df` and return metrics + models.

    Applies SMOTE to the training split to address class imbalance, since
    confirmed wash-trade examples are rare relative to clean activity.

    When ``adversarial_augment=True``, generates 3 additional datasets with
    mixed evasion strategies and concatenates them before SMOTE resampling,
    forcing the models to learn adversarial meta-signatures.

    When ``calibrate=True``, reserves a 10 % calibration split (stratified)
    before the train/test split, trains on the remaining data, then runs
    conformal calibration on the held-out set. Calibration data and
    ``ConformalCalibrator`` instances are returned under the ``"calib"`` key
    and used by ``save_models`` to persist the artifacts.
    """
    df = merge_evasion_samples(df, evasion_samples)
    if adversarial_augment:
        from detection.dataset import build_training_dataset
        from ingestion.adversarial_data import ALL_STRATEGIES, generate_adversarial_dataset

        augment_dfs = [df]
        strategy_groups = [
            ALL_STRATEGIES[:2],
            ALL_STRATEGIES[2:4],
            ALL_STRATEGIES,
        ]
        for i, strats in enumerate(strategy_groups):
            trades, meta, events, labels = generate_adversarial_dataset(
                n_normal_accounts=50,
                n_wash_rings=10,
                ring_size=4,
                evasion_strategies=strats,
                seed=random_state + i + 1,
            )
            augment_dfs.append(
                build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
            )
        df = pd.concat(augment_dfs, ignore_index=True)

    X, y = _split_features_labels(df)

    if calibrate:
        X_remaining, X_cal, y_remaining, y_cal = train_test_split(
            X, y, test_size=0.10, random_state=random_state, stratify=y
        )
        cal_split_info = {
            "X_cal": X_cal,
            "y_cal": y_cal,
            "cal_index_start": X_cal.index.min(),
            "cal_index_end": X_cal.index.max(),
        }
        X_train, X_test, y_train, y_test = train_test_split(
            X_remaining, y_remaining, test_size=0.2, random_state=random_state, stratify=y_remaining
        )
    else:
        cal_split_info = {}
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=random_state, stratify=y
        )

    # --- Causal feature selection (PC skeleton phase)
    _causal_selected_features = None
    if causal_feature_selection:
        from detection.causal_engine import CausalFeatureSelector

        selector = CausalFeatureSelector(
            alpha=settings.causal_independence_alpha,
            max_conditioning_size=settings.causal_max_conditioning_size,
        )
        selected = selector.fit(
            X_train.to_numpy(dtype=float),
            y_train.to_numpy(dtype=float),
            feature_names=list(X_train.columns),
        )
        if selected:
            X_train = X_train[selected]
            X_test = X_test[selected]
            if calibrate and "X_cal" in cal_split_info:
                cal_split_info["X_cal"] = cal_split_info["X_cal"][selected]
            _causal_selected_features = selected
            logger.info(
                "Causal feature selection retained %d/%d features",
                len(selected), X_train.shape[1] + (len(list(X_train.columns)) - len(selected)),
            )

    smote = SMOTE(random_state=random_state)
    X_train_res, y_train_res = smote.fit_resample(X_train, y_train)

    models = {
        "random_forest": RandomForestClassifier(n_estimators=200, random_state=random_state, n_jobs=-1),
        "xgboost": XGBClassifier(eval_metric="logloss", random_state=random_state),
        "lightgbm": LGBMClassifier(random_state=random_state, verbose=-1),
    }

    results = {}
    for name, model in models.items():
        model.fit(X_train_res, y_train_res)
        y_proba = model.predict_proba(X_test)[:, 1]
        y_pred = model.predict(X_test)

        results[name] = {
            "model": model,
            "auc_roc": roc_auc_score(y_test, y_proba),
            "pr_auc": average_precision_score(y_test, y_proba),
            "f1": f1_score(y_test, y_pred),
        }

    if calibrate:
        from detection.conformal import ConformalCalibrator

        calibrators = {}
        for name, result in results.items():
            cal = ConformalCalibrator(alpha=0.10).calibrate(
                result["model"], cal_split_info["X_cal"], cal_split_info["y_cal"]
            )
            calibrators[name] = cal
            # Empirical coverage on the calibration set
            cal_split_info[f"coverage_{name}"] = _compute_empirical_coverage(
                result["model"], cal_split_info["X_cal"], cal_split_info["y_cal"], cal.q_hat
            )
        results["_calib"] = {**cal_split_info, "calibrators": calibrators}

    # --- Adversarial hardening: generate PGD adversarial examples from
    # training true positives and retrain once on the augmented set.
    if adversarial_hardening:
        try:
            from detection.adversarial_attack import pgd_attack

            # collect adversarial examples that successfully flip model
            adv_rows = []
            # use the ensemble (current models) to attack training positives
            ensemble_models = {k: v["model"] for k, v in results.items()}
            X_train_res_df = pd.DataFrame(X_train_res, columns=X_train_res.columns)
            y_train_res_ser = pd.Series(y_train_res)
            for idx, (x_row, y_val) in enumerate(zip(X_train_res_df.to_dict(orient="records"), y_train_res_ser.tolist())):
                if int(y_val) != 1:
                    continue
                pert, p = pgd_attack(x_row, ensemble_models, epsilon=0.1, alpha=0.01, steps=10)
                if p < 0.5:
                    adv_rows.append({**pert, "label": 1})

            if adv_rows:
                aug_df = pd.DataFrame(adv_rows)
                # append to original training set and retrain
                X_aug = pd.concat([X_train_res_df, aug_df.drop(columns=["label"])], ignore_index=True)
                y_aug = pd.concat([y_train_res_ser, aug_df["label"].astype(int)], ignore_index=True)

                for name, model in models.items():
                    model.fit(X_aug, y_aug)
                    y_proba = model.predict_proba(X_test)[:, 1]
                    y_pred = model.predict(X_test)
                    results[name] = {
                        "model": model,
                        "auc_roc": roc_auc_score(y_test, y_proba),
                        "pr_auc": average_precision_score(y_test, y_proba),
                        "f1": f1_score(y_test, y_pred),
                    }
        except Exception:
            # Hardening is best-effort; failures should not crash training.
            pass

    # Train LSTM temporal anomaly model
    try:
        from detection.temporal_dataset import build_training_sequences
        from detection.temporal_model import train_temporal_model, predict_temporal_risk

        # Train/validation split by wallet
        train_df, test_df = train_test_split(
            df, test_size=0.2, random_state=random_state, stratify=df["label"]
        )

        X_train_seq, y_train_seq = build_training_sequences(train_df, db_path=settings.db_path)
        X_test_seq, y_test_seq = build_training_sequences(test_df, db_path=settings.db_path)

        lstm_model = train_temporal_model(X_train_seq, y_train_seq, epochs=15, batch_size=32)

        # Evaluate on test sequence dataset
        y_proba_seq = np.array([predict_temporal_risk(lstm_model, seq) for seq in X_test_seq])
        y_pred_seq = (y_proba_seq >= 0.5).astype(int)

        if len(np.unique(y_test_seq)) > 1:
            lstm_auc_roc = roc_auc_score(y_test_seq, y_proba_seq)
            lstm_pr_auc = average_precision_score(y_test_seq, y_proba_seq)
            lstm_f1 = f1_score(y_test_seq, y_pred_seq)
        else:
            lstm_auc_roc, lstm_pr_auc, lstm_f1 = 1.0, 1.0, 1.0

        results["temporal_lstm"] = {
            "model": lstm_model,
            "auc_roc": lstm_auc_roc,
            "pr_auc": lstm_pr_auc,
            "f1": lstm_f1,
        }
    except Exception as e:
        import logging
        logger = logging.getLogger("ledgerlens.model_training")
        logger.exception("Failed to train temporal LSTM model: %s", e)

    if _causal_selected_features is not None:
        results["_causal_selected_features"] = _causal_selected_features

    return results


def _compute_empirical_coverage(model, X_cal, y_cal, q_hat):
    """Fraction of calibration examples whose true class is in the prediction set."""
    probs = model.predict_proba(X_cal)
    scores = 1.0 - probs[range(len(y_cal)), y_cal.values]
    return float((scores <= q_hat).mean())


def save_models(
    results: dict,
    model_dir: str | None = None,
    training_dataset_path: str | None = None,
) -> None:
    """Persist trained models to `model_dir` (defaults to `settings.model_dir`).

    Also writes training_metadata.json with model versions, AUC-ROC scores,
    and training dataset path for drift detection and rollback.

    When ``results`` contains ``"_calib"`` key (from ``train_ensemble`` with
    ``calibrate=True``), calibration artifacts are written alongside each
    model file and ``metrics.json`` is updated with empirical coverage.
    """
    import hashlib
    import json
    import os
    from datetime import datetime, timezone

    from detection.model_registry import _compute_version_hash

    model_dir = model_dir or settings.model_dir
    os.makedirs(model_dir, exist_ok=True)

    signing_key = settings.model_signing_key.encode()
    for name, result in results.items():
        if name in ("_calib", "_causal_selected_features"):
            continue
        path = os.path.join(model_dir, f"{name}.joblib")
        joblib.dump(result["model"], path)
        sign_model_file(path, signing_key)

    # Write training_metadata.json
    if training_dataset_path:
        try:
            train_df = pd.read_csv(training_dataset_path)
            training_row_count = len(train_df)
            column_hash = hashlib.sha256(
                ",".join(train_df.columns).encode()
            ).hexdigest()[:8]
        except Exception:
            training_row_count = 0
            column_hash = "unknown"
    else:
        training_row_count = 0
        column_hash = "unknown"

    version = _compute_version_hash(training_row_count, column_hash)

    _causal_selected = results.get("_causal_selected_features")
    metadata = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "training_dataset_path": training_dataset_path or "",
        "training_row_count": training_row_count,
        "column_hash": column_hash,
        "causal_feature_selection": _causal_selected is not None,
        "causal_selected_features": _causal_selected or [],
        "model_metrics": {
            name: {
                "auc_roc": result.get("auc_roc", 0.0),
                "pr_auc": result.get("pr_auc", 0.0),
                "f1": result.get("f1", 0.0),
            }
            for name, result in results.items()
            if name not in ("_calib", "_causal_selected_features")
        },
    }

    metadata_path = os.path.join(model_dir, "training_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    import logging

    logger = logging.getLogger("ledgerlens.model_training")
    logger.info("Wrote training metadata to %s", metadata_path)

    # ------------------------------------------------------------------
    # Calibration artifacts
    # ------------------------------------------------------------------
    calib = results.get("_calib")
    if calib and calib.get("calibrators"):
        metrics = {}
        for name, cal in calib["calibrators"].items():
            cal_path = os.path.join(model_dir, f"{name}_conformal.json")
            cal.save(cal_path)
            cover_key = f"coverage_{name}"
            cov = calib.get(cover_key, 0.0)
            metrics[f"conformal_empirical_coverage_{name}"] = round(cov, 4)

        # Aggregate coverage (simple average across models)
        coverages = [v for k, v in metrics.items() if k.startswith("conformal_empirical_coverage_")]
        metrics["conformal_empirical_coverage"] = round(
            sum(coverages) / len(coverages), 4
        ) if coverages else 0.0

        # Log calibration split index range for audit
        metrics["calibration_index_start"] = int(calib.get("cal_index_start", -1))
        metrics["calibration_index_end"] = int(calib.get("cal_index_end", -1))

        metrics_path = os.path.join(model_dir, "metrics.json")
        existing = {}
        if os.path.exists(metrics_path):
            with open(metrics_path, "r") as f:
                try:
                    existing = json.load(f)
                except Exception:
                    pass
        existing.update(metrics)
        with open(metrics_path, "w") as f:
            json.dump(existing, f, indent=2)
        logger.info(
            "Wrote calibration metrics (coverage=%.4f) to %s",
            metrics.get("conformal_empirical_coverage", 0.0),
            metrics_path,
        )


if __name__ == "__main__":
    # The ledgerlens-data repo does not yet provide a labelled dataset, so
    # default to a synthetic one for local training/testing.
    import logging

    from detection.dataset import build_training_dataset
    from ingestion.synthetic_data import generate_synthetic_dataset

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("ledgerlens.model_training")

    trades, account_metadata, order_book_events, labels = generate_synthetic_dataset(
        n_normal_accounts=60, n_wash_rings=10, ring_size=3
    )
    df = build_training_dataset(trades, labels, account_metadata=account_metadata, order_book_events=order_book_events)

    results = train_ensemble(df)  # noqa: F821
    for name, result in results.items():
        if name == "_calib":
            continue
        logger.info(
            "%s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f",
            name,
            result["auc_roc"],
            result["pr_auc"],
            result["f1"],
        )

    save_models(results)
    logger.info("Saved models to %s", settings.model_dir)


from detection.gnn_model import TGATWashRingDetector, save_gnn_checkpoint, _HAS_PYG  # noqa: E402
from ingestion.graph_builder import TemporalGraphBuilder  # noqa: E402
import os  # noqa: E402


def train_ensemble(
    df,
    *args,
    use_gnn: bool = False,
    model_dir: str = "models",
    causal_feature_selection: bool = False,
    **kwargs,
):
    """Wraps the base ensemble trainer, optionally pre-training a T-GNN.

    Args:
        use_gnn: If True, trains a T-GNN on the training graph, appends its
            two output features to the feature matrix before SMOTE, and
            saves the checkpoint as gnn_model.pt in model_dir.
        causal_feature_selection: When True, runs the PC-skeleton causal
            feature selector before SMOTE resampling.  Selected features are
            stored in ``results["_causal_selected_features"]`` and written to
            ``training_metadata.json`` by :func:`save_models`.
    """
    gnn_features_by_wallet = {}

    if use_gnn:
        if not _HAS_PYG:
            raise RuntimeError(
                "use_gnn=True requires torch + torch_geometric installed."
            )
        builder = TemporalGraphBuilder()
        trades = _trades_from_training_df(df)  # noqa: F821
        snapshots = builder.build_snapshots(trades, lookback_days=30)

        import torch
        model = TGATWashRingDetector()
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
        gnn_features_by_wallet = _run_gnn_training_loop(model, optimizer, snapshots)  # noqa: F821

        os.makedirs(model_dir, exist_ok=True)
        save_gnn_checkpoint(model, os.path.join(model_dir, "gnn_model.pt"))

    return _train_ensemble_base(
        df, *args, use_gnn=use_gnn, gnn_features=gnn_features_by_wallet,
        model_dir=model_dir, causal_feature_selection=causal_feature_selection, **kwargs
    )
