"""Unit tests for detection.federated.admission.

Covers the admission-record store directly, independent of the server (see
tests/test_federated_sybil.py for the integration-level tests that exercise
this through FederatedAggregationServer.register_participant/submit_update).
"""

import pytest

from detection.federated.admission import (
    AdmissionRecord,
    admit_participant,
    get_admission,
    is_admitted,
    list_admissions,
    revoke_admission,
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "admission.db")


# ── admit_participant ─────────────────────────────────────────────────────────

def test_admit_participant_returns_record(db):
    record = admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    assert record == AdmissionRecord(
        participant_id="op-1",
        max_n_samples=1000,
        admitted_at=record.admitted_at,
        admitted_by="alice",
        revoked=False,
    )


def test_admit_participant_persists(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    fetched = get_admission("op-1", db_path=db)
    assert fetched is not None
    assert fetched.participant_id == "op-1"
    assert fetched.max_n_samples == 1000
    assert fetched.admitted_by == "alice"
    assert fetched.revoked is False


def test_admit_participant_rejects_non_positive_max_n_samples(db):
    with pytest.raises(ValueError, match="positive"):
        admit_participant("op-1", 0, admitted_by="alice", db_path=db)
    with pytest.raises(ValueError, match="positive"):
        admit_participant("op-1", -5, admitted_by="alice", db_path=db)


def test_admit_participant_rejects_empty_id(db):
    with pytest.raises(ValueError, match="participant_id"):
        admit_participant("", 1000, admitted_by="alice", db_path=db)


def test_re_admitting_updates_ceiling(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    admit_participant("op-1", 5000, admitted_by="bob", db_path=db)
    fetched = get_admission("op-1", db_path=db)
    assert fetched.max_n_samples == 5000
    assert fetched.admitted_by == "bob"


def test_re_admitting_clears_prior_revocation(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    revoke_admission("op-1", db_path=db)
    assert is_admitted("op-1", db_path=db) is False

    admit_participant("op-1", 2000, admitted_by="alice", db_path=db)
    assert is_admitted("op-1", db_path=db) is True
    assert get_admission("op-1", db_path=db).revoked is False


# ── get_admission / is_admitted ───────────────────────────────────────────────

def test_get_admission_returns_none_for_unknown_participant(db):
    assert get_admission("nonexistent", db_path=db) is None


def test_is_admitted_false_for_unknown_participant(db):
    assert is_admitted("nonexistent", db_path=db) is False


def test_is_admitted_true_after_admission(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    assert is_admitted("op-1", db_path=db) is True


def test_is_admitted_false_after_revocation(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    revoke_admission("op-1", db_path=db)
    assert is_admitted("op-1", db_path=db) is False


# ── revoke_admission ───────────────────────────────────────────────────────────

def test_revoke_admission_returns_false_for_unknown_participant(db):
    assert revoke_admission("nonexistent", db_path=db) is False


def test_revoke_admission_returns_true_and_sets_revoked(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    assert revoke_admission("op-1", db_path=db) is True
    record = get_admission("op-1", db_path=db)
    assert record.revoked is True


def test_revoking_twice_is_idempotent(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    assert revoke_admission("op-1", db_path=db) is True
    assert revoke_admission("op-1", db_path=db) is True  # still finds the row, re-revokes
    assert is_admitted("op-1", db_path=db) is False


# ── list_admissions ───────────────────────────────────────────────────────────

def test_list_admissions_empty_store(db):
    assert list_admissions(db_path=db) == []


def test_list_admissions_returns_all(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    admit_participant("op-2", 2000, admitted_by="bob", db_path=db)
    admit_participant("op-3", 3000, admitted_by="carol", db_path=db)

    records = list_admissions(db_path=db)
    ids = {r.participant_id for r in records}
    assert ids == {"op-1", "op-2", "op-3"}


def test_list_admissions_includes_revoked(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    revoke_admission("op-1", db_path=db)
    records = list_admissions(db_path=db)
    assert len(records) == 1
    assert records[0].revoked is True


# ── Isolation between participant_ids ─────────────────────────────────────────

def test_admissions_are_independent_per_participant(db):
    admit_participant("op-1", 1000, admitted_by="alice", db_path=db)
    admit_participant("op-2", 2000, admitted_by="alice", db_path=db)
    revoke_admission("op-1", db_path=db)

    assert is_admitted("op-1", db_path=db) is False
    assert is_admitted("op-2", db_path=db) is True
    assert get_admission("op-2", db_path=db).max_n_samples == 2000
