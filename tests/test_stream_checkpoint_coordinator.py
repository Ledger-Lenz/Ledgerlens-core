"""Tests for ingestion/stream_checkpoint.py — the unified cursor/window-state
checkpoint that replaces the two independently-triggered checkpoints
previously advanced by cli.py's `stream` command.

Coverage maps directly to the issue's acceptance criteria:
- Low-throughput crash recovery: no trade lost across a simulated crash when
  sustained volume is well under the old event-count threshold.
- High-throughput cadence: the event-count trigger dominates under load,
  matching the pre-fix checkpoint cadence (no throughput regression).
- Arbitrary-crash-point fuzz: a crash injected at any statement within the
  atomic transaction never leaves the cursor and window state partially
  updated relative to each other.
- Desync detection: an artificially corrupted checkpoint row is flagged via
  log + metric on load.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

from detection.rolling_window import RollingWindowState, RollingWindowStore
from ingestion.checkpoint import FlushPolicy
from ingestion.data_models import Asset, Trade
from ingestion.stream_checkpoint import StreamCheckpointCoordinator


def _trade(wallet: str, idx: int) -> Trade:
    return Trade(
        id=f"t-{idx}",
        ledger_close_time=datetime.now(timezone.utc),
        base_account=wallet,
        counter_account="GCOUNTER",
        base_asset=Asset(code="XLM"),
        counter_asset=Asset(
            code="USDC", issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
        ),
        base_amount=100.0,
        counter_amount=200.0,
        price=2.0,
        base_is_seller=True,
    )


def _cursor(idx: int) -> str:
    return f"{1000 + idx}-0"


# ---------------------------------------------------------------------------
# Acceptance criterion 1: low-throughput crash recovery
# ---------------------------------------------------------------------------


class TestLowThroughputCrashRecovery:
    def test_no_trade_lost_across_simulated_crash(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db_path = f.name
            store = RollingWindowStore(db_path=db_path)
            state = RollingWindowState()
            coordinator = StreamCheckpointCoordinator(
                rolling_store=store,
                flush_policy=FlushPolicy(max_events=100, max_seconds=10.0),
                now=0.0,
            )

            fake_now = 0.0
            flushed_cursors = []
            wallets = {f"GWALLET{i % 3}" for i in range(15)}

            # 18 trades at ~1 trade/2s = well under 100 trades/10s. This
            # produces >= 2 time-based flushes (the event-count trigger,
            # 100, never fires) with trailing un-checkpointed trades left
            # over to simulate the crash gap.
            for i in range(18):
                wallet = f"GWALLET{i % 3}"
                state.add_trade(wallet, _trade(wallet, i))
                fake_now += 2.0
                if coordinator.on_trade_processed(_cursor(i), state, now=fake_now):
                    flushed_cursors.append(_cursor(i))

            assert len(flushed_cursors) >= 2, (
                "expected >= 2 time-based flushes under sustained throughput "
                "well below the 100-event count trigger"
            )
            last_committed_cursor = flushed_cursors[-1]
            # There must be at least one trade processed after the last
            # flush that was never made durable -- otherwise this test isn't
            # exercising the crash gap at all.
            assert last_committed_cursor != _cursor(17)

            # Simulate a crash: the trailing trades after the last flush are
            # never checkpointed. A fresh process reopens the same DB file.
            fresh_state = RollingWindowState()
            fresh_store = RollingWindowStore(db_path=db_path)
            fresh_store.load_all(fresh_state)
            fresh_coordinator = StreamCheckpointCoordinator(
                rolling_store=fresh_store,
                flush_policy=FlushPolicy(max_events=100, max_seconds=10.0),
            )
            recovered_cursor = fresh_coordinator.load_cursor(
                actual_wallet_count=fresh_state.active_wallets
            )

            # The recovered cursor must be exactly the last durably
            # committed one -- never a later, unflushed trade's cursor
            # (which would mean the cursor is durably ahead of window state).
            assert recovered_cursor == last_committed_cursor
            # Every wallet touched at or before the last successful flush
            # must be present in the reloaded window state.
            assert fresh_state.active_wallets == len(wallets)
            for wallet in wallets:
                assert fresh_state.get_wallet_window(wallet) is not None


# ---------------------------------------------------------------------------
# Acceptance criterion 2: no throughput regression under high load
# ---------------------------------------------------------------------------


class TestHighThroughputCadence:
    def test_event_count_trigger_dominates_under_high_load(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = RollingWindowStore(db_path=f.name)
            state = RollingWindowState()
            coordinator = StreamCheckpointCoordinator(
                rolling_store=store,
                flush_policy=FlushPolicy(max_events=100, max_seconds=10.0),
                now=0.0,
            )

            fake_now = 0.0
            flush_count = 0
            for i in range(1000):
                wallet = f"GWALLET{i % 20}"
                state.add_trade(wallet, _trade(wallet, i))
                fake_now += 0.01  # 100 events/second, far above 100/10s
                if coordinator.on_trade_processed(_cursor(i), state, now=fake_now):
                    flush_count += 1

            # Under sustained high throughput the 100-event trigger always
            # fires before the 10s timer (100 events take 1s here), so the
            # checkpoint cadence matches the pre-fix window-state-only
            # cadence exactly -- no added flushes, no throughput regression.
            assert flush_count == 10


# ---------------------------------------------------------------------------
# Acceptance criterion 3: crash at an arbitrary point in the atomic write
# ---------------------------------------------------------------------------


class _CrashAfterN:
    """Proxies a sqlite3.Connection; the (n+1)-th ``execute()`` call raises.

    Simulates a process crash before ``COMMIT`` is reached: no explicit
    rollback semantics are assumed beyond what SQLite itself guarantees for
    an uncommitted transaction on next open.
    """

    def __init__(self, conn: sqlite3.Connection, n: int) -> None:
        self._conn = conn
        self._n = n
        self._count = 0

    def execute(self, *args, **kwargs):
        self._count += 1
        if self._count > self._n:
            raise sqlite3.OperationalError("simulated crash")
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestArbitraryCrashPoint:
    def test_crash_at_any_statement_never_leaves_partial_checkpoint(self):
        num_wallets = 5
        # Statements inside one flush(): 1 BEGIN IMMEDIATE + num_wallets
        # upserts + 1 cursor upsert.
        total_statements = num_wallets + 2

        for n in range(total_statements + 1):
            with tempfile.NamedTemporaryFile(suffix=".db") as f:
                db_path = f.name
                store = RollingWindowStore(db_path=db_path)
                state = RollingWindowState()
                coordinator = StreamCheckpointCoordinator(
                    rolling_store=store, flush_policy=FlushPolicy(max_events=100, max_seconds=10.0)
                )

                # Establish a known-good prior checkpoint (no fault injection).
                for i in range(num_wallets):
                    state.add_trade(f"GWALLET{i}", _trade(f"GWALLET{i}", i))
                coordinator.flush(_cursor(0), state)

                # Attempt a second checkpoint with a fault injected at
                # statement n; wrap store.connect() so `flush()`'s internal
                # `with self._store.connect() as conn` yields a crashing proxy.
                state.add_trade("GWALLET_NEW", _trade("GWALLET_NEW", 999))
                real_connect = store.connect

                def crashing_connect(n=n):
                    from contextlib import contextmanager

                    @contextmanager
                    def _cm():
                        with real_connect() as conn:
                            yield _CrashAfterN(conn, n)

                    return _cm()

                store.connect = crashing_connect
                try:
                    coordinator.flush(_cursor(1), state)
                finally:
                    store.connect = real_connect

                # Inspect with a completely fresh connection/store.
                verify_store = RollingWindowStore(db_path=db_path)
                verify_state = RollingWindowState()
                verify_store.load_all(verify_state)
                verify_coordinator = StreamCheckpointCoordinator(
                    rolling_store=verify_store,
                    flush_policy=FlushPolicy(max_events=100, max_seconds=10.0),
                )
                recovered_cursor = verify_coordinator.load_cursor(
                    actual_wallet_count=verify_state.active_wallets
                )

                is_old_state = (
                    recovered_cursor == _cursor(0)
                    and verify_state.active_wallets == num_wallets
                )
                is_new_state = (
                    recovered_cursor == _cursor(1)
                    and verify_state.active_wallets == num_wallets + 1
                )
                assert is_old_state or is_new_state, (
                    f"crash injected at statement {n}/{total_statements} left "
                    f"a mixed/partial checkpoint: cursor={recovered_cursor!r}, "
                    f"wallet_count={verify_state.active_wallets}"
                )


# ---------------------------------------------------------------------------
# Acceptance criterion 4: desync detection (defense in depth)
# ---------------------------------------------------------------------------


class TestDesyncDetection:
    def test_load_cursor_flags_artificially_constructed_desync(self, monkeypatch, caplog):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db_path = f.name
            store = RollingWindowStore(db_path=db_path)
            state = RollingWindowState()
            coordinator = StreamCheckpointCoordinator(
                rolling_store=store, flush_policy=FlushPolicy(max_events=100, max_seconds=10.0)
            )
            for i in range(3):
                state.add_trade(f"GWALLET{i}", _trade(f"GWALLET{i}", i))
            coordinator.flush(_cursor(0), state)

            fake_metrics = MagicMock()
            monkeypatch.setattr(
                "ingestion.stream_checkpoint.get_metrics", lambda: fake_metrics
            )

            # actual_wallet_count deliberately mismatches the 3 recorded at
            # flush time, simulating storage-layer corruption/tampering that
            # bypassed the atomic transaction.
            with caplog.at_level("ERROR"):
                cursor = coordinator.load_cursor(actual_wallet_count=1)

            assert cursor == _cursor(0)
            assert any("desync" in rec.message.lower() for rec in caplog.records)
            fake_metrics.checkpoint_desync_detected_total.inc.assert_called_once()

    def test_load_cursor_does_not_false_positive_on_normal_recovery(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db_path = f.name
            store = RollingWindowStore(db_path=db_path)
            state = RollingWindowState()
            coordinator = StreamCheckpointCoordinator(
                rolling_store=store, flush_policy=FlushPolicy(max_events=100, max_seconds=10.0)
            )
            for i in range(4):
                state.add_trade(f"GWALLET{i}", _trade(f"GWALLET{i}", i))
            coordinator.flush(_cursor(0), state)

            fake_metrics = MagicMock()
            monkeypatch.setattr(
                "ingestion.stream_checkpoint.get_metrics", lambda: fake_metrics
            )

            fresh_state = RollingWindowState()
            fresh_store = RollingWindowStore(db_path=db_path)
            fresh_store.load_all(fresh_state)
            fresh_coordinator = StreamCheckpointCoordinator(
                rolling_store=fresh_store, flush_policy=FlushPolicy(max_events=100, max_seconds=10.0)
            )
            cursor = fresh_coordinator.load_cursor(
                actual_wallet_count=fresh_state.active_wallets
            )

            assert cursor == _cursor(0)
            fake_metrics.checkpoint_desync_detected_total.inc.assert_not_called()


# ---------------------------------------------------------------------------
# Reset (--reset-cursor wiring)
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_unified_checkpoint(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = RollingWindowStore(db_path=f.name)
            state = RollingWindowState()
            state.add_trade("GWALLET0", _trade("GWALLET0", 0))
            coordinator = StreamCheckpointCoordinator(
                rolling_store=store, flush_policy=FlushPolicy(max_events=100, max_seconds=10.0)
            )
            coordinator.flush(_cursor(0), state)
            assert coordinator.load_cursor(actual_wallet_count=1) == _cursor(0)

            coordinator.reset()

            assert coordinator.load_cursor(actual_wallet_count=1) is None
