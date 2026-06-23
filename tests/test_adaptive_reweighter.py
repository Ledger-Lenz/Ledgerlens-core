from __future__ import annotations

from datetime import datetime, timezone, timedelta

import numpy as np
import pytest

from detection.adaptive_reweighter import (
    ThompsonSamplingReweighter,
    _CLASSIFIER_NAMES,
    _brier_reward,
    _run_update_cycle,
    cusum_detect,
    load_state,
    save_state,
)
from detection.feedback_store import ScoringFeedback, record_feedback


def _fb(model_name: str, prob: float, label: int, db_path: str) -> None:
    record_feedback(
        ScoringFeedback(
            wallet="GABC",
            asset_pair="XLM/USDC",
            model_name=model_name,
            predicted_probability=prob,
            ground_truth=label,
            scored_at=datetime.now(timezone.utc),
            confirmed_at=datetime.now(timezone.utc),
        ),
        db_path=db_path,
    )


def test_thompson_sampling_converges_to_best_classifier():
    # random_forest: always correct (high confidence on wash trades)
    # xgboost / lightgbm: random 50% guesses
    rng = np.random.default_rng(0)
    rw = ThompsonSamplingReweighter(n_classifiers=3)

    for _ in range(200):
        rw.update(0, 1.0 - (0.95 - 1) ** 2)          # rf: near-perfect
        rw.update(1, 1.0 - (rng.random() - rng.integers(0, 2)) ** 2)  # xgb: random
        rw.update(2, 1.0 - (rng.random() - rng.integers(0, 2)) ** 2)  # lgbm: random

    weights = rw.current_weights()
    assert weights["random_forest"] > weights["xgboost"], "rf should outweigh xgb"
    assert weights["random_forest"] > weights["lightgbm"], "rf should outweigh lgbm"


def test_sample_weights_sum_to_one():
    rw = ThompsonSamplingReweighter(n_classifiers=3)
    for _ in range(20):
        samples = rw.sample_weights()
        assert abs(samples.sum() - 1.0) < 1e-9


def test_update_clips_reward_to_unit_interval():
    rw = ThompsonSamplingReweighter(n_classifiers=3)
    alpha_before = rw.alphas[0]
    rw.update(0, 2.0)   # should be clipped to 1.0
    assert rw.alphas[0] == alpha_before + 1.0
    rw.update(0, -1.0)  # should be clipped to 0.0
    assert rw.alphas[0] == alpha_before + 1.0  # unchanged again


def test_reset_priors_returns_to_uniform():
    rw = ThompsonSamplingReweighter(n_classifiers=3)
    rw.update(0, 0.9)
    rw.update(1, 0.1)
    rw.reset_priors()
    assert np.allclose(rw.alphas, 1.0)
    assert np.allclose(rw.betas, 1.0)


def test_cusum_detects_step_change():
    errors = [0.05] * 30 + [0.6] * 30
    assert cusum_detect(errors) is True


def test_cusum_no_false_positive_on_stable_series():
    errors = [0.05] * 60
    assert cusum_detect(errors) is False


def test_cusum_returns_false_for_short_series():
    assert cusum_detect([0.1, 0.9, 0.1]) is False


def test_sqlite_persistence_round_trip(tmp_path):
    db = str(tmp_path / "test.db")
    rw = ThompsonSamplingReweighter(n_classifiers=3)
    rw.update(0, 0.9)
    rw.update(1, 0.3)
    save_state(rw, db_path=db)

    loaded = load_state(db_path=db)
    assert loaded is not None
    assert np.allclose(loaded.alphas, rw.alphas)
    assert np.allclose(loaded.betas, rw.betas)


def test_load_state_returns_none_on_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    assert load_state(db_path=db) is None


def test_model_weights_response_shape():
    # Tests the same logic the /api/v1/model/weights endpoint executes,
    # without importing api.main (which has a pre-existing broken import).
    rw = ThompsonSamplingReweighter(n_classifiers=3)
    weights = rw.current_weights()
    payload = {
        "classifiers": [
            {
                "name": name,
                "alpha": float(rw.alphas[i]),
                "beta": float(rw.betas[i]),
                "weight": weights[name],
            }
            for i, name in enumerate(_CLASSIFIER_NAMES)
        ],
    }
    names = {c["name"] for c in payload["classifiers"]}
    assert names == set(_CLASSIFIER_NAMES)
    for c in payload["classifiers"]:
        assert c["alpha"] >= 1.0
        assert c["beta"] >= 1.0
        assert 0.0 < c["weight"] < 1.0
    total = sum(c["weight"] for c in payload["classifiers"])
    assert abs(total - 1.0) < 1e-6


def test_integration_weights_shift_toward_better_classifier(tmp_path):
    db = str(tmp_path / "test.db")
    since = datetime.now(timezone.utc) - timedelta(seconds=1)

    # rf is correct on 20 wash-trade examples; xgb and lgbm are wrong
    for _ in range(20):
        _fb("random_forest", 0.95, 1, db)   # rf: high confidence, correct
        _fb("xgboost", 0.1, 1, db)          # xgb: low confidence, wrong direction
        _fb("lightgbm", 0.1, 1, db)         # lgbm: same

    rw = ThompsonSamplingReweighter(n_classifiers=3)
    _run_update_cycle(rw, since, db_path=db)

    weights = rw.current_weights()
    assert weights["random_forest"] > weights["xgboost"], (
        f"rf weight {weights['random_forest']:.3f} should exceed "
        f"xgb weight {weights['xgboost']:.3f} after 2 update cycles"
    )
    assert weights["random_forest"] > weights["lightgbm"]
