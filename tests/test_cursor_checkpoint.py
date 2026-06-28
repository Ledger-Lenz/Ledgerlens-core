import json
import os

import pytest

from ingestion.checkpoint import (
    CursorCheckpoint,
    FlushPolicy,
    resolve_checkpoint_path,
)


def test_load_valid_checkpoint(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(
        json.dumps(
            {
                "paging_token": "12345678901234-0",
                "recorded_at": "2026-06-24T09:00:00Z",
                "ledger_sequence": 50123456,
            }
        )
    )

    assert CursorCheckpoint(path).load() == "12345678901234-0"


def test_load_missing_checkpoint(tmp_path):
    assert CursorCheckpoint(tmp_path / "missing.json").load() is None


def test_load_corrupt_or_empty_checkpoint(tmp_path):
    path = tmp_path / "cursor.json"
    checkpoint = CursorCheckpoint(path)
    path.write_text("{not-json")
    assert checkpoint.load() is None
    path.write_text("")
    assert checkpoint.load() is None


def test_load_invalid_paging_token(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({"paging_token": "../../bad"}))
    assert CursorCheckpoint(path).load() is None


def test_save_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "cursor.json"
    original = '{"paging_token":"100-0"}\n'
    path.write_text(original)
    checkpoint = CursorCheckpoint(path)

    def fail_replace(source, destination):
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", fail_replace)
    checkpoint.save("200-0", ledger_sequence=2)

    assert path.read_text() == original


def test_save_writes_expected_payload_and_permissions(tmp_path):
    path = tmp_path / "cursor.json"
    CursorCheckpoint(path).save("123-0", ledger_sequence=42)

    payload = json.loads(path.read_text())
    assert payload["paging_token"] == "123-0"
    assert payload["ledger_sequence"] == 42
    assert payload["recorded_at"].endswith("Z")
    assert path.stat().st_mode & 0o777 == 0o600


def test_delete_removes_checkpoint(tmp_path):
    path = tmp_path / "cursor.json"
    checkpoint = CursorCheckpoint(path)
    checkpoint.save("123-0")
    checkpoint.delete()
    assert not path.exists()


def test_flush_policy_event_boundary():
    policy = FlushPolicy(max_events=100, max_seconds=10.0)
    assert not policy.should_flush(99, 0.0, 9.9)
    assert policy.should_flush(100, 0.0, 1.0)


def test_flush_policy_time_boundary():
    policy = FlushPolicy(max_events=100, max_seconds=10.0)
    assert policy.should_flush(1, 5.0, 15.0)


def test_checkpoint_path_cannot_escape_data_directory(tmp_path):
    with pytest.raises(ValueError):
        resolve_checkpoint_path(tmp_path.parent / "outside.json", tmp_path)
