import pandas as pd

from detection.robustness_eval import compute_robustness_report
from tests.test_adversarial_attack import DummyModel
from detection.feature_engineering import FEATURE_NAMES
from detection.storage import get_latest_robustness_report


def make_models():
    return {"dummy": DummyModel(w=5.0, b=-1.0)}


def make_df():
    rows = []
    for _ in range(10):
        rows.append({f: 0.2 for f in FEATURE_NAMES})
    df = pd.DataFrame(rows)
    df["label"] = 1
    return df


def test_compute_report_and_persistence():
    models = make_models()
    df = make_df()
    report = compute_robustness_report(models, df, n_samples=20, epsilon=0.1, steps=5, seed=1)
    assert hasattr(report, "model_version")
    assert isinstance(report.asr, dict)
    assert 0.0 <= min(report.asr.values()) <= 1.0
    assert report.mean_map >= 0.0
    assert report.certified_radius >= 0.0

    persisted = get_latest_robustness_report()
    assert persisted is not None
    assert persisted.get("model_version") == report.model_version
