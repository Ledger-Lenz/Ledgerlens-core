"""Tests for ``detection.webhook_queue``.

All source imports are at module level.  Every test receives an isolated
SQLite database via the ``db_path`` fixture so there is no shared state
between tests.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from detection.webhook_queue import (
    MAX_ATTEMPTS,
    _connect,
    enqueue,
    get_dead_letters,
    get_due_deliveries,
    init_db,
    mark_delivered,
    mark_failed,
)


@pytest.fixture
def db_path(tmp_path):
    """Return a fresh per-test SQLite path; init_db is called by each test
    that needs the schema so the fixture itself stays lightweight."""
    return str(tmp_path / "queue.db")


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def test_enqueue_creates_pending_delivery(db_path):
    """enqueue() inserts a row with status='pending' and attempt_count=0."""
    init_db(db_path)
    enqueue("sub-123", {"wallet": "GABC", "score": 85}, db_path)

    due = get_due_deliveries(db_path=db_path)
    assert len(due) == 1
    assert due[0].subscriber_id == "sub-123"
    assert due[0].status == "pending"
    assert due[0].attempt_count == 0


def test_enqueue_stores_payload(db_path):
    """The payload is serialised to JSON and round-trips correctly."""
    init_db(db_path)
    payload = {"wallet": "GABC", "score": 85, "benford_flag": True}
    enqueue("sub-123", payload, db_path)

    due = get_due_deliveries(db_path=db_path)
    assert json.loads(due[0].payload_json) == payload


# ---------------------------------------------------------------------------
# get_due_deliveries
# ---------------------------------------------------------------------------


def test_get_due_deliveries_only_returns_pending(db_path):
    """Delivered rows are excluded from get_due_deliveries()."""
    init_db(db_path)
    enqueue("sub-1", {"w": "A"}, db_path)
    enqueue("sub-2", {"w": "B"}, db_path)

    due = get_due_deliveries(db_path=db_path)
    assert len(due) == 2

    mark_delivered(due[0].id, 200, db_path)
    due = get_due_deliveries(db_path=db_path)
    assert len(due) == 1


def test_get_due_deliveries_respects_limit(db_path):
    """The ``limit`` argument caps how many rows are returned."""
    init_db(db_path)
    for i in range(5):
        enqueue(f"sub-{i}", {"i": i}, db_path)

    due = get_due_deliveries(limit=3, db_path=db_path)
    assert len(due) == 3


# ---------------------------------------------------------------------------
# mark_delivered
# ---------------------------------------------------------------------------


def test_mark_delivered_sets_status(db_path):
    """mark_delivered() moves the item out of the pending queue."""
    init_db(db_path)
    enqueue("sub-1", {"w": "A"}, db_path)
    delivery = get_due_deliveries(db_path=db_path)[0]

    mark_delivered(delivery.id, 200, db_path)

    assert len(get_due_deliveries(db_path=db_path)) == 0


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------


def test_mark_failed_increments_attempt_and_sets_backoff(db_path):
    """First failure increments attempt_count to 1 and sets next_attempt_at
    to now + 2^1 * 5 = 10 seconds."""
    init_db(db_path)
    enqueue("sub-1", {"w": "A"}, db_path)
    delivery = get_due_deliveries(db_path=db_path)[0]

    now = datetime.now(timezone.utc)
    with patch("detection.webhook_queue.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mark_failed(delivery.id, "HTTP 500", db_path=db_path)

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT attempt_count, next_attempt_at, last_error, status "
            "FROM webhook_delivery_queue WHERE id = ?",
            (delivery.id,),
        ).fetchone()

    assert row[0] == 1                                          # attempt_count
    assert row[3] == "pending"                                  # status unchanged
    assert row[1] == (now + timedelta(seconds=10)).isoformat()  # 2^1 * 5 = 10 s
    assert row[2] == "HTTP 500"                                 # last_error


def test_mark_failed_moves_to_dead_after_max_attempts(db_path):
    """After MAX_ATTEMPTS failures the item is promoted to dead-letter."""
    init_db(db_path)
    enqueue("sub-1", {"w": "A"}, db_path)
    delivery = get_due_deliveries(db_path=db_path)[0]

    # Pre-seed attempt_count so the next mark_failed reaches MAX_ATTEMPTS.
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_delivery_queue SET attempt_count = ? WHERE id = ?",
            (MAX_ATTEMPTS - 1, delivery.id),
        )
        conn.commit()

    mark_failed(delivery.id, "final error", db_path=db_path)

    assert len(get_due_deliveries(db_path=db_path)) == 0

    dead = get_dead_letters(db_path=db_path)
    assert len(dead) == 1
    assert dead[0].status == "dead"
    assert dead[0].attempt_count == MAX_ATTEMPTS
    assert dead[0].last_error == "final error"


def test_mark_failed_exponential_backoff_caps_at_one_hour(db_path):
    """Backoff is capped at 3600 seconds regardless of the attempt count."""
    init_db(db_path)
    enqueue("sub-1", {"w": "A"}, db_path)
    delivery = get_due_deliveries(db_path=db_path)[0]

    # Set attempt_count high enough that 2^11 * 5 > 3600.
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_delivery_queue SET attempt_count = 10 WHERE id = ?",
            (delivery.id,),
        )
        conn.commit()

    now = datetime.now(timezone.utc)
    with patch("detection.webhook_queue.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mark_failed(delivery.id, "error", max_attempts=15, db_path=db_path)

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT attempt_count, next_attempt_at "
            "FROM webhook_delivery_queue WHERE id = ?",
            (delivery.id,),
        ).fetchone()

    assert row[0] == 11                                              # incremented
    assert row[1] == (now + timedelta(seconds=3600)).isoformat()    # cap applied


# ---------------------------------------------------------------------------
# Dead letters
# ---------------------------------------------------------------------------


def test_get_dead_letters_returns_only_dead(db_path):
    """get_dead_letters() excludes delivered and pending items."""
    init_db(db_path)
    enqueue("sub-1", {"w": "A"}, db_path)
    enqueue("sub-2", {"w": "B"}, db_path)

    deliveries = get_due_deliveries(db_path=db_path)
    mark_delivered(deliveries[0].id, 200, db_path)

    # Force the second delivery to dead-letter on the next mark_failed call.
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_delivery_queue SET attempt_count = ? WHERE id = ?",
            (MAX_ATTEMPTS - 1, deliveries[1].id),
        )
        conn.commit()
    mark_failed(deliveries[1].id, "dead", db_path=db_path)

    dead = get_dead_letters(db_path=db_path)
    assert len(dead) == 1
    assert dead[0].id == deliveries[1].id
