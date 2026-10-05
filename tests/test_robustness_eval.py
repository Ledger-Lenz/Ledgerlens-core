import pandas as pd
import pytest
import numpy as np

import detection.adversarial_attack as adversarial_attack
import detection.robustness_eval as robustness_eval
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


@pytest.mark.parametrize(("attacked_probability", "expected"), [(0.9, True), (0.1, False)])
def test_promotion_gate_runs_feature_attacks_and_enforces_evasion_limit(
    monkeypatch, attacked_probability, expected
):
    class FixedModel:
        def __init__(self, probability):
            self.probability = probability

        def predict_proba(self, X):
            return np.tile([1.0 - self.probability, self.probability], (len(X), 1))

    examples = pd.DataFrame([{**{name: 0.1 for name in FEATURE_NAMES}, "label": 1}
                             for _ in range(2)])
    monkeypatch.setattr(
        robustness_eval,
        "generate_adversarial_dataset",
        lambda **kwargs: (pd.DataFrame(), {}, pd.DataFrame(), {}),
    )
    monkeypatch.setattr(
        robustness_eval,
        "build_training_dataset",
        lambda *args, **kwargs: examples,
    )
    attack_calls = []

    def attack(features, models, **kwargs):
        attack_calls.append(kwargs)
        return features, attacked_probability

    monkeypatch.setattr(adversarial_attack, "pgd_attack", attack)
    monkeypatch.setattr(robustness_eval, "pgd_attack", attack)

    report = robustness_eval.evaluate_promotion_robustness(
        {"random_forest": FixedModel(0.9)}, sample_size=2
    )

    assert report["passed"] is expected
    assert report["models"]["random_forest"]["evasion_rate"] == (0.0 if expected else 1.0)
    assert len(attack_calls) == 2
    assert all(call["steps"] > 0 for call in attack_calls)
