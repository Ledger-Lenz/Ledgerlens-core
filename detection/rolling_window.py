"""Stateful rolling-window trade store for incremental per-wallet feature computation.

Maintains per-wallet deques of trades within a 24-hour horizon.  On each
new trade, expired entries (older than 24 h) are evicted before the trade
is appended.  Callers can then query sub-windows (1 h, 4 h, 24 h) for
feature engineering.

Persistence is handled by :class:`RollingWindowStore`, which serialises
window state to the ``rolling_window_checkpoints`` SQLite table so the
streamer survives restarts without losing accumulated history.

Security notes
--------------
- Checkpoint JSON is produced via ``trade.model_dump()`` (Pydantic), not
  pickle, preventing code execution on load.
- A hard cap of ``MAX_TRADES_PER_WALLET_WINDOW`` trades per wallet protects
  against unbounded memory growth from high-volume accounts.
- Checkpoint contents must not be exposed via the public API.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional

from config.settings import settings
from ingestion.data_models import Asset, Trade, TradeType

logger = logging.getLogger("ledgerlens.rolling_window")

WINDOW_HOURS = [1, 4, 24]
MAX_TRADES_PER_WALLET_WINDOW = 10_000

_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rolling_window_checkpoints (
    wallet      TEXT NOT NULL,
    trades_json TEXT NOT NULL,
    last_score  INTEGER,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wallet)
);
"""


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


class WalletWindow:
    """Per-wallet deque of trades covering up to 24 hours.

    Trades are kept in chronological order.  On each :meth:`add`, trades
    older than 24 h are evicted from the left.  A hard cap of
    :data:`MAX_TRADES_PER_WALLET_WINDOW` entries prevents unbounded growth;
    the oldest trade is dropped when the cap is reached and a WARNING is
    logged.

    The ``_last_score`` field caches the most-recently emitted score so
    :class:`~detection.model_inference.IncrementalScorer` can compute the
    delta without querying storage.
    """

    def __init__(self) -> None:
        self._trades: Deque[Trade] = deque()
        self._last_score: Optional[int] = None
        self._last_scored_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add(self, trade: Trade) -> None:
        """Append *trade* and evict stale entries older than 24 h."""
        self._evict(hours=24)
        if len(self._trades) >= MAX_TRADES_PER_WALLET_WINDOW:
            logger.warning(
                "WalletWindow cap (%d) reached; dropping oldest trade",
                MAX_TRADES_PER_WALLET_WINDOW,
            )
            self._trades.popleft()
        self._trades.append(trade)

    def get(self, hours: int) -> List[Trade]:
        """Return trades whose ``ledger_close_time`` falls within the last *hours*."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [t for t in self._trades if _as_utc(t.ledger_close_time) >= cutoff]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict(self, hours: int) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        while self._trades and _as_utc(self._trades[0].ledger_close_time) < cutoff:
            self._trades.popleft()

    # ------------------------------------------------------------------
    # Serialisation helpers (for SQLite checkpoint)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "trades": [t.model_dump(mode="json") for t in self._trades],
            "last_score": self._last_score,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WalletWindow":
        ww = cls()
        for td in data.get("trades", []):
            # Reconstruct nested Asset objects
            td["base_asset"] = Asset(**td["base_asset"])
            td["counter_asset"] = Asset(**td["counter_asset"])
            ww._trades.append(Trade(**td))
        ww._last_score = data.get("last_score")
        return ww


def _as_utc(dt: datetime) -> datetime:
    """Return *dt* as UTC-aware, assuming UTC if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RollingWindowState:
    """In-memory store of per-wallet :class:`WalletWindow` objects.

    Thread-safety: not thread-safe by design.  The streaming loop runs in a
    single thread; the graceful-shutdown handler calls
    :meth:`checkpoint_all` from a signal handler and must do so before the
    main loop exits.
    """

    def __init__(self) -> None:
        self._wallets: Dict[str, WalletWindow] = {}

    def add_trade(self, wallet: str, trade: Trade) -> None:
        """Add *trade* to *wallet*'s window, creating the window if absent."""
        if wallet not in self._wallets:
            self._wallets[wallet] = WalletWindow()
        self._wallets[wallet].add(trade)

    def get_window(self, wallet: str, hours: int) -> List[Trade]:
        """Return trades within the last *hours* for *wallet*."""
        if wallet not in self._wallets:
            return []
        return self._wallets[wallet].get(hours)

    def get_wallet_window(self, wallet: str) -> Optional[WalletWindow]:
        return self._wallets.get(wallet)

    @property
    def active_wallets(self) -> int:
        """Number of wallets with at least one trade in their 24-h window."""
        return len(self._wallets)

    def wallets(self) -> Dict[str, WalletWindow]:
        return self._wallets


class RollingWindowStore:
    """SQLite persistence for :class:`RollingWindowState`.

    Each wallet occupies one row in ``rolling_window_checkpoints``.  On
    :meth:`save_state` the full 24-h trade list is JSON-serialised via
    Pydantic's ``model_dump``; on :meth:`load_state` it is reconstructed
    without ``pickle``.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with _connect(self._db_path) as conn:
            conn.executescript(_CHECKPOINT_SCHEMA)
            conn.commit()

    def save_state(self, wallet: str, window: WalletWindow) -> None:
        """Upsert *window* for *wallet*."""
        data = json.dumps(window.to_dict())
        now = datetime.now(timezone.utc).isoformat()
        with _connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO rolling_window_checkpoints (wallet, trades_json, last_score, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    trades_json = excluded.trades_json,
                    last_score  = excluded.last_score,
                    updated_at  = excluded.updated_at
                """,
                (wallet, data, window._last_score, now),
            )
            conn.commit()

    def load_state(self, wallet: str) -> Optional[WalletWindow]:
        """Return the persisted :class:`WalletWindow` for *wallet*, or ``None``."""
        with _connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT trades_json, last_score FROM rolling_window_checkpoints WHERE wallet = ?",
                (wallet,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("Corrupt checkpoint for wallet %s; ignoring", wallet)
            return None
        ww = WalletWindow.from_dict(data)
        ww._last_score = row[1]
        return ww

    def save_all(self, state: RollingWindowState) -> None:
        """Checkpoint every wallet in *state*."""
        for wallet, window in state.wallets().items():
            self.save_state(wallet, window)

    def load_all(self, state: RollingWindowState) -> None:
        """Populate *state* from all persisted checkpoints."""
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT wallet, trades_json, last_score FROM rolling_window_checkpoints"
            ).fetchall()
        for wallet, trades_json, last_score in rows:
            try:
                data = json.loads(trades_json)
            except json.JSONDecodeError:
                logger.warning("Corrupt checkpoint for wallet %s; skipping", wallet)
                continue
            ww = WalletWindow.from_dict(data)
            ww._last_score = last_score
            state._wallets[wallet] = ww
        logger.info("Loaded %d wallet windows from checkpoint", len(rows))
