"""Train the Random Forest / XGBoost / LightGBM wash-trading ensemble.

Expects a feature DataFrame (see `feature_engineering.build_feature_vector`)
with a binary `label` column (1 = confirmed wash trade pattern). Trained
models are written to `settings.model_dir` for `model_inference` to load.
"""

import joblib
import pandas as pd
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
    X = df[FEATURE_NAMES]
    y = df["label"]
    return X, y


def train_ensemble(df: pd.DataFrame, random_state: int = 42) -> dict:
    """Train RF, XGBoost, and LightGBM classifiers on `df` and return metrics + models.

    Applies SMOTE to the training split to address class imbalance, since
    confirmed wash-trade examples are rare relative to clean activity.
    """
    X, y = _split_features_labels(df)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=random_state, stratify=y
    )

    smote = SMOTE(random_state=random_state)
    X_train_res, y_train_res = smote.fit_resample(X_train, y_train)

    models = {
        "random_forest": RandomForestClassifier(n_estimators=200, random_state=random_state, n_jobs=-1),
        "xgboost": XGBClassifier(eval_metric="logloss", random_state=random_state),
        "lightgbm": LGBMClassifier(random_state=random_state),
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

    return results


def save_models(results: dict, model_dir: str | None = None) -> None:
    """Persist trained models to `model_dir` (defaults to `settings.model_dir`)."""
    import os

    model_dir = model_dir or settings.model_dir
    os.makedirs(model_dir, exist_ok=True)

    for name, result in results.items():
        path = os.path.join(model_dir, f"{name}.joblib")
        joblib.dump(result["model"], path)


if __name__ == "__main__":
    # TODO: load labelled training data from the ledgerlens-data repo
    raise SystemExit("Provide a labelled feature DataFrame via the ledgerlens-data pipeline")
