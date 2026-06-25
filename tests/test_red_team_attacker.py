"""Tests for the real-time adversarial red team loop (issue #66)."""

import base64
import json
import os
import threading

import numpy as np
import pytest

from detection.red_team.attacker import GeneticAttacker, evaluate_score
from detection.red_team.evasion_logger import (
    MODEL_EVASION_EVENT,
    count_evasions,
    get_evasion_events,
    log_evasion,
    maybe_trigger_hardening,
)
from detection.red_team.runner import run_red_team_loop, start_red_team_loop

# A tiny, well-understood feature space with realistic on-chain bounds.
CONSTRAINTS = {
    "trade_count": {"min": 1.0, "max": 1000.0, "mutable": True},
    "volume": {"min": 1.0, "max": 1_000_000.0, "mutable": True},
    "wash_signal": {"min": 0.0, "max": 1.0, "mutable": True},
    "account_age_days": {"min": 0.0, "max": 3650.0, "mutable": False},
}
FEATURES = list(CONSTRAINTS.keys())


def wash_score(features: dict) -> float:
    """Monotone 0-100 score: high for blatant wash trades, low as features shrink."""
    v = (features["volume"] - 1.0) / (1_000_000.0 - 1.0)
    t = (features["trade_count"] - 1.0) / (1000.0 - 1.0)
    w = features["wash_signal"]
    return 100.0 * (0.34 * v + 0.33 * t + 0.33 * w)


SEED = {"trade_count": 1000.0, "volume": 1_000_000.0, "wash_signal": 1.0, "account_age_days": 30.0}
SEED_ARRAY = np.array([SEED[f] for f in FEATURES], dtype=float)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "redteam.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", path)
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "db_path", path)
    return path


# ---------------------------------------------------------------------------
# GeneticAttacker
# ---------------------------------------------------------------------------


def test_seed_starts_high_risk():
    assert evaluate_score(wash_score, SEED) >= 80.0


def test_genetic_attacker_finds_evasion_within_200_generations():
    attacker = GeneticAttacker(wash_score, CONSTRAINTS, population_size=50, seed=7)
    best, score = attacker.evolve(SEED_ARRAY, n_generations=200)
    assert score < 30.0
    assert evaluate_score(wash_score, attacker.to_dict(best)) == pytest.approx(score, abs=1e-6)


def test_constraints_are_enforced():
    attacker = GeneticAttacker(wash_score, CONSTRAINTS, population_size=40, seed=3)
    best, _ = attacker.evolve(SEED_ARRAY, n_generations=150)
    evolved = attacker.to_dict(best)
    # No evolved sample may violate the realistic on-chain bounds.
    assert evolved["trade_count"] >= 1.0
    assert evolved["volume"] > 0.0
    assert 0.0 <= evolved["wash_signal"] <= 1.0
    # Immutable features stay pinned to the seed value.
    assert evolved["account_age_days"] == SEED["account_age_days"]


def test_attacker_rejects_mismatched_seed_length():
    attacker = GeneticAttacker(wash_score, CONSTRAINTS, seed=1)
    with pytest.raises(ValueError):
        attacker.evolve(np.array([1.0, 2.0]), n_generations=5)


# ---------------------------------------------------------------------------
# Evasion logger
# ---------------------------------------------------------------------------


def test_evasion_events_persisted_and_queryable(db_path):
    log_evasion(SEED, {**SEED, "wash_signal": 0.0}, 95.0, 12.0, 8, db_path=db_path)
    log_evasion(SEED, SEED, 95.0, 88.0, 200, db_path=db_path)  # not an evasion

    events = get_evasion_events(db_path=db_path)
    assert len(events) == 2
    assert count_evasions(db_path=db_path) == 1
    evasions = get_evasion_events(only_evasions=True, db_path=db_path)
    assert len(evasions) == 1
    assert evasions[0]["evasion_score"] == 12.0
    assert evasions[0]["attacker_generation"] == 8


# ---------------------------------------------------------------------------
# Hardening trigger + webhook
# ---------------------------------------------------------------------------


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setenv(
        "LEDGERLENS_WEBHOOK_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )


def test_model_evasion_webhook_fires_after_trigger(db_path, webhook_env):
    from detection.webhook_queue import get_due_deliveries
    from detection.webhook_registry import register_subscriber

    register_subscriber("https://example.com/hook", "s3cret", min_score=0, db_path=db_path)

    for i in range(3):
        log_evasion(SEED, {**SEED, "wash_signal": 0.0}, 90.0, 10.0, i + 1, db_path=db_path)

    captured = {}

    def retrain(samples):
        captured["n"] = len(samples)

    # Below the trigger count -> no fire.
    assert maybe_trigger_hardening(n_trigger=5, retrain_callback=retrain, db_path=db_path) is False
    assert get_due_deliveries(db_path=db_path) == []

    # At/above the trigger count -> webhook + retrain callback fire.
    fired = maybe_trigger_hardening(n_trigger=3, retrain_callback=retrain, db_path=db_path)
    assert fired is True
    assert captured["n"] == 3

    deliveries = get_due_deliveries(db_path=db_path)
    assert len(deliveries) == 1
    payload = json.loads(deliveries[0].payload_json)
    assert payload["event_type"] == MODEL_EVASION_EVENT
    assert payload["evasion_count"] == 3


# ---------------------------------------------------------------------------
# Live robustness metrics + endpoint provider
# ---------------------------------------------------------------------------


def test_live_robustness_metrics(db_path):
    from detection.robustness_eval import live_robustness_metrics

    log_evasion(SEED, {**SEED, "wash_signal": 0.0}, 95.0, 10.0, 5, db_path=db_path)
    log_evasion(SEED, {**SEED, "wash_signal": 0.0}, 95.0, 15.0, 25, db_path=db_path)
    log_evasion(SEED, SEED, 95.0, 90.0, 200, db_path=db_path)  # not an evasion

    metrics = live_robustness_metrics(db_path=db_path)
    assert set(metrics) == {"evasion_rate_24h", "mean_generations_to_evade", "hardening_delta"}
    assert metrics["evasion_rate_24h"] == pytest.approx(2 / 3)
    assert metrics["mean_generations_to_evade"] == pytest.approx(15.0)  # mean(5, 25)
    assert metrics["hardening_delta"] == 0.0  # no retrain recorded yet


# ---------------------------------------------------------------------------
# Continuous runner / background thread
# ---------------------------------------------------------------------------


def test_red_team_loop_runs_in_background_without_blocking(db_path, tmp_path):
    seed_path = str(tmp_path / "seeds.json")
    with open(seed_path, "w", encoding="utf-8") as fh:
        json.dump([SEED, SEED], fh)

    stop_event = threading.Event()
    thread = start_red_team_loop(
        wash_score,
        seed_path,
        CONSTRAINTS,
        poll_interval_seconds=1,
        n_seeds_per_round=2,
        n_generations=120,
        max_iterations=1,
        stop_event=stop_event,
        db_path=db_path,
        seed=11,
    )

    thread.join(timeout=60)
    stop_event.set()
    assert not thread.is_alive()  # the loop completed without blocking
    # The round drove the seeds below the evasion threshold and logged them.
    assert count_evasions(db_path=db_path) >= 1


def test_run_red_team_loop_returns_round_count(db_path, tmp_path):
    seed_path = str(tmp_path / "seeds.json")
    with open(seed_path, "w", encoding="utf-8") as fh:
        json.dump([SEED], fh)

    rounds = run_red_team_loop(
        wash_score,
        seed_path,
        CONSTRAINTS,
        poll_interval_seconds=0,  # no real wait between rounds in the test
        n_seeds_per_round=1,
        n_generations=120,
        max_iterations=2,
        db_path=db_path,
        seed=5,
    )
    assert rounds == 2
