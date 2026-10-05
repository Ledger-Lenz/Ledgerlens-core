"""Cache for expensive aggregate risk-score queries.

Entries are invalidated by actual score writes rather than a blind TTL: every
lookup reads a cheap *score generation* token (``MAX(id)`` of ``risk_scores``,
an index-only lookup on the primary key) and discards any entry computed under
an older generation.  Because the token lives in the database, writes from the
pipeline process invalidate the API process's cache too.  ``max_age`` bounds
staleness for changes that don't advance the token (e.g. retention pruning).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any

from config.settings import settings
from detection.storage import _connect, init_db

DEFAULT_MAX_AGE_SECONDS = 60.0


def score_generation(db_path: str | None = None) -> tuple[str, int]:
    """Return a token that changes whenever a new risk score is committed."""
    path = db_path or settings.db_path
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM risk_scores").fetchone()
    return path, int(row[0])


@dataclass
class CacheResult:
    value: Any
    hit: bool
    age_seconds: float


@dataclass
class _Entry:
    value: Any
    generation: Hashable
    computed_at: float


class AggregateCache:
    def __init__(
        self,
        max_age: float = DEFAULT_MAX_AGE_SECONDS,
        generation_fn: Callable[[], Hashable] = score_generation,
    ) -> None:
        self._max_age = max_age
        self._generation_fn = generation_fn
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> CacheResult:
        generation = self._generation_fn()
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
        if (
            entry is not None
            and entry.generation == generation
            and now - entry.computed_at <= self._max_age
        ):
            return CacheResult(entry.value, hit=True, age_seconds=now - entry.computed_at)

        value = compute()
        with self._lock:
            self._entries[key] = _Entry(value, generation, time.monotonic())
        return CacheResult(value, hit=False, age_seconds=0.0)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


aggregate_cache = AggregateCache()
