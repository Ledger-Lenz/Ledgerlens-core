"""Event bus dead-letter and replay flow (#1007)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from config.settings import settings
from detection.event_bus import (
    EventBusDeadLetterStore,
    KafkaRiskScoreBus,
    NullEventBus,
)
from detection.risk_score import RiskScore


@pytest.fixture
def store(tmp_path):
    s = EventBusDeadLetterStore(db_path=str(tmp_path / "dlq.db"))
    with patch("detection.event_bus._dead_letter_store", s):
        yield s


@pytest.fixture
def failing_kafka_bus():
    producer = MagicMock()
    producer.produce.side_effect = Exception("broker unavailable")
    producer.flush.return_value = 0
    with patch.dict("sys.modules", {"confluent_kafka": MagicMock(Producer=MagicMock(return_value=producer))}):
        bus = KafkaRiskScoreBus(bootstrap_servers="localhost:9092", topic="t")
    yield bus, producer


def _score():
    return RiskScore(
        wallet="GA1",
        asset_pair="XLM/USDC",
        score=85,
        benford_flag=True,
        ml_flag=False,
        confidence=90,
        timestamp=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
    )


def test_persistent_failure_is_dead_lettered_not_dropped(store, failing_kafka_bus, monkeypatch):
    bus, producer = failing_kafka_bus
    monkeypatch.setattr(settings, "event_bus_retry_backoff_seconds", 0)

    result = bus.publish([_score()])

    assert result.failed == 1
    assert producer.produce.call_count == settings.event_bus_max_retries
    [entry] = store.entries()
    assert entry.backend == "kafka"
    assert entry.key == b"GA1:XLM/USDC"
    assert b'"risk_score.updated"' in entry.value
    assert "broker unavailable" in entry.error
    assert store.count() == 1
    assert store.oldest_age_seconds() >= 0


def test_dead_letter_metrics_exposed(store):
    import api.metrics  # noqa: F401  (registers the gauges)
    from prometheus_client import REGISTRY

    store.add("kafka", b"k", b"v", "boom")
    assert REGISTRY.get_sample_value("ledgerlens_event_bus_dead_letter_events") == 1.0
    assert REGISTRY.get_sample_value("ledgerlens_event_bus_dead_letter_oldest_age_seconds") >= 0.0


def test_replay_after_fix_republishes_and_clears(store, failing_kafka_bus, monkeypatch):
    bus, producer = failing_kafka_bus
    monkeypatch.setattr(settings, "event_bus_retry_backoff_seconds", 0)
    bus.publish([_score()])
    original = store.entries()[0]

    # Fault fixed: the broker accepts messages again.
    producer.produce.side_effect = None
    producer.produce.reset_mock()
    result = bus.replay_dead_letters(store)

    assert (result.replayed, result.failed, result.remaining) == (1, 0, 0)
    producer.produce.assert_called_once_with("t", key=original.key, value=original.value)


def test_failed_replay_keeps_entry_and_counts_attempt(store, failing_kafka_bus, monkeypatch):
    bus, _ = failing_kafka_bus
    monkeypatch.setattr(settings, "event_bus_retry_backoff_seconds", 0)
    bus.publish([_score()])

    result = bus.replay_dead_letters(store)

    assert (result.replayed, result.failed, result.remaining) == (0, 1, 1)
    assert store.entries()[0].replay_attempts == 1


def test_null_bus_replay_leaves_entries(store):
    store.add("kafka", b"k", b"v", "boom")
    result = NullEventBus().replay_dead_letters(store)
    assert (result.replayed, result.failed, result.remaining) == (0, 1, 1)


def test_cli_replay(store, failing_kafka_bus, monkeypatch):
    import cli

    bus, producer = failing_kafka_bus
    monkeypatch.setattr(settings, "event_bus_retry_backoff_seconds", 0)
    bus.publish([_score()])
    producer.produce.side_effect = None

    runner = CliRunner()
    with patch("detection.event_bus.get_event_bus", return_value=bus):
        listed = runner.invoke(cli.app, ["event-bus-replay", "--list"])
        assert listed.exit_code == 0 and "1 dead-lettered event(s)." in listed.output
        replayed = runner.invoke(cli.app, ["event-bus-replay"])

    assert replayed.exit_code == 0, replayed.output
    assert "Replayed 1, failed 0, remaining 0." in replayed.output
    assert store.count() == 0
