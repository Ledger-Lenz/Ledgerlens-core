"""Tests for the aggregate risk-score cache and its invalidation."""

from __future__ import annotations

from datetime import datetime, timezone

from api.score_cache import AggregateCache, score_generation
from detection.risk_score import RiskScore
from detection.storage import save_scores


def _score(wallet: str, pair: str, score: int) -> RiskScore:
    return RiskScore(
        wallet=wallet,
        asset_pair=pair,
        score=score,
        benford_flag=False,
        ml_flag=score >= 70,
        confidence=90,
        timestamp=datetime.now(timezone.utc),
    )


def test_cache_miss_computes_then_hits(tmp_path):
    db = str(tmp_path / "scores.db")
    cache = AggregateCache(generation_fn=lambda: score_generation(db))
    calls: list[int] = []

    def compute():
        calls.append(1)
        return ["ranking"]

    first = cache.get_or_compute("k", compute)
    second = cache.get_or_compute("k", compute)

    assert first.hit is False and first.value == ["ranking"]
    assert second.hit is True and second.value == ["ranking"]
    assert second.age_seconds >= 0.0
    assert len(calls) == 1


def test_score_update_invalidates_entry(tmp_path):
    db = str(tmp_path / "scores.db")
    cache = AggregateCache(generation_fn=lambda: score_generation(db))
    values = iter([["before"], ["after"]])

    assert cache.get_or_compute("k", lambda: next(values)).value == ["before"]
    save_scores([_score("G" + "A" * 55, "XLM/USDC", 80)], db)

    result = cache.get_or_compute("k", lambda: next(values))
    assert result.hit is False
    assert result.value == ["after"]


def test_max_age_bounds_staleness():
    cache = AggregateCache(max_age=0.0, generation_fn=lambda: 1)
    cache.get_or_compute("k", lambda: 1)
    assert cache.get_or_compute("k", lambda: 2).value == 2
