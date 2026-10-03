"""Chain-of-custody tests for scoring events (Issue #1032)."""

import pytest

from audit.scoring_events import make_scoring_event
from storage.audit_log import (
    InvalidEventSignatureError,
    get_all_entries,
    ingest_scoring_event,
)


@pytest.fixture(autouse=True)
def _audit_secret(monkeypatch):
    monkeypatch.setenv("LEDGERLENS_AUDIT_SECRET", "x" * 48)


@pytest.fixture
def audit_db(tmp_path) -> str:
    return str(tmp_path / "audit_custody.db")


def _event():
    return make_scoring_event(
        wallet="G" + "A" * 55,
        namespace_id="default",
        score=42,
        previous_score=None,
        feature_snapshot={"benford_chi_square_24h": 1.2},
        model_version="v1.0.0",
        triggered_by="ingestion",
    )


def test_event_is_signed_at_generation():
    event = _event()
    assert event.signature
    assert event.verify_signature()


def test_valid_event_is_ingested(audit_db):
    ingest_scoring_event(_event().to_dict(), db_path=audit_db)
    assert any(e["event_type"] == "score_computed" for e in get_all_entries(audit_db))


def test_tampered_in_transit_event_is_rejected(audit_db):
    payload = _event().to_dict()
    payload["score"] = 5  # attacker lowers the score in transit
    with pytest.raises(InvalidEventSignatureError):
        ingest_scoring_event(payload, db_path=audit_db)
    assert not any(e["event_type"] == "score_computed" for e in get_all_entries(audit_db))


def test_unsigned_event_is_rejected(audit_db):
    payload = _event().to_dict()
    payload["signature"] = ""
    with pytest.raises(InvalidEventSignatureError):
        ingest_scoring_event(payload, db_path=audit_db)
