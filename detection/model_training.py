"""Train the Random Forest / XGBoost / LightGBM wash-trading ensemble.

Expects a feature DataFrame (see `feature_engineering.build_feature_vector`)
with a binary `label` column (1 = confirmed wash trade pattern). Trained
models are written to `settings.model_dir` for `model_inference` to load.
"""

import joblib
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


def train_ensemble(df: pd.DataFrame, random_state: int = 42, adversarial_augment: bool = True, adversarial_hardening: bool = False) -> dict:
    """Train RF, XGBoost, and LightGBM classifiers on `df` and return metrics + models.

    Applies SMOTE to the training split to address class imbalance, since
    confirmed wash-trade examples are rare relative to clean activity.

    When ``adversarial_augment=True``, generates 3 additional datasets with
    mixed evasion strategies and concatenates them before SMOTE resampling,
    forcing the models to learn adversarial meta-signatures.
    """
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
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=random_state, stratify=y
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

    return results


def save_models(results: dict, model_dir: str | None = None, training_dataset_path: str | None = None) -> None:
    """Persist trained models to `model_dir` (defaults to `settings.model_dir`).

    Also writes training_metadata.json with model versions, AUC-ROC scores,
    and training dataset path for drift detection and rollback.
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
        path = os.path.join(model_dir, f"{name}.joblib")
        joblib.dump(result["model"], path)
        sign_model_file(path, signing_key)

    # Write training_metadata.json
    if training_dataset_path:
        try:
            # Compute training dataset hash for versioning
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

    metadata = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "training_dataset_path": training_dataset_path or "",
        "training_row_count": training_row_count,
        "column_hash": column_hash,
        "model_metrics": {
            name: {
                "auc_roc": result.get("auc_roc", 0.0),
                "pr_auc": result.get("pr_auc", 0.0),
                "f1": result.get("f1", 0.0),
            }
            for name, result in results.items()
        },
    }

    metadata_path = os.path.join(model_dir, "training_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    import logging
    logger = logging.getLogger("ledgerlens.model_training")
    logger.info("Wrote training metadata to %s", metadata_path)


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

    results = train_ensemble(df)
    for name, result in results.items():
        logger.info(
            "%s: AUC-ROC=%.3f PR-AUC=%.3f F1=%.3f",
            name,
            result["auc_roc"],
            result["pr_auc"],
            result["f1"],
        )

    save_models(results)
    logger.info("Saved models to %s", settings.model_dir)


from detection.gnn_model import TGATWashRingDetector, save_gnn_checkpoint, _HAS_PYG
from ingestion.graph_builder import TemporalGraphBuilder
import os


def train_ensemble(df, *args, use_gnn: bool = False, model_dir: str = "models", **kwargs):
    """Wraps the base ensemble trainer, optionally pre-training a T-GNN.

    Args:
        use_gnn: If True, trains a T-GNN on the training graph, appends its
            two output features to the feature matrix before SMOTE, and
            saves the checkpoint as gnn_model.pt in model_dir.
    """
    gnn_features_by_wallet = {}

    if use_gnn:
        if not _HAS_PYG:
            raise RuntimeError(
                "use_gnn=True requires torch + torch_geometric installed."
            )
        builder = TemporalGraphBuilder()
        trades = _trades_from_training_df(df)
        snapshots = builder.build_snapshots(trades, lookback_days=30)

        import torch
        model = TGATWashRingDetector()
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
        gnn_features_by_wallet = _run_gnn_training_loop(model, optimizer, snapshots)

        os.makedirs(model_dir, exist_ok=True)
        save_gnn_checkpoint(model, os.path.join(model_dir, "gnn_model.pt"))

    return _train_ensemble_base(
        df, *args, use_gnn=use_gnn, gnn_features=gnn_features_by_wallet,
        model_dir=model_dir, **kwargs
    )
