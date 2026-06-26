"""Durable Horizon paging-token checkpoints."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_PAGING_TOKEN_RE = re.compile(r"^\d+-\d+$")

try:  # pragma: no cover - exercised on POSIX CI, unavailable on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def validate_cursor(cursor: str) -> str:
    """Return *cursor* when it is a valid Horizon cursor, otherwise raise."""
    if cursor == "now" or _PAGING_TOKEN_RE.fullmatch(cursor):
        return cursor
    raise ValueError(f"Malformed Horizon paging token: {cursor!r}")


def resolve_checkpoint_path(path: str | Path, data_directory: str | Path) -> Path:
    """Resolve *path* and ensure it remains inside *data_directory*."""
    data_root = Path(data_directory).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError as exc:
        raise ValueError(
            f"Cursor checkpoint path {candidate} must be inside data directory {data_root}"
        ) from exc
    return candidate


@dataclass(frozen=True)
class FlushPolicy:
    """Limits how long and how many events may remain uncheckpointed."""

    max_events: int = 100
    max_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.max_events <= 0:
            raise ValueError("max_events must be positive")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")

    def should_flush(
        self, events_since_flush: int, last_flush_time: float, now: float
    ) -> bool:
        """Return whether either the event-count or elapsed-time limit was met."""
        return (
            events_since_flush >= self.max_events
            or now - last_flush_time >= self.max_seconds
        )


class CursorCheckpoint:
    """Persist a Horizon paging token in a small, atomically replaced JSON file."""

    def __init__(self, path: Path):
        """Create a checkpoint at an absolute path.

        Writes use a sibling temporary file followed by :func:`os.replace`, so
        readers see either the previous complete checkpoint or the new one.
        """
        self.path = path.expanduser().resolve()
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(self._lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _warn_if_permissions_wide(self) -> None:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            logger.warning(
                "Cursor checkpoint %s has permissions %03o; expected 600",
                self.path,
                mode,
            )

    def load(self) -> str | None:
        """Return the stored token, or ``None`` when absent, unreadable, or corrupt.

        Reading is protected by an advisory lock. Malformed JSON and invalid
        paging-token values are logged and treated as a missing checkpoint.
        """
        try:
            with self._locked():
                if not self.path.exists():
                    return None
                self._warn_if_permissions_wide()
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                token = payload["paging_token"]
                if not isinstance(token, str):
                    raise ValueError("paging_token must be a string")
                return validate_cursor(token)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Ignoring unreadable cursor checkpoint %s: %s", self.path, exc)
            return None

    def save(self, paging_token: str, ledger_sequence: int | None = None) -> None:
        """Atomically save a token using write-to-temp plus :func:`os.replace`.

        If replacement fails, the prior checkpoint remains untouched. File
        errors are logged so ingestion can continue without crashing.
        """
        try:
            token = validate_cursor(paging_token)
            payload = {
                "paging_token": token,
                "recorded_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "ledger_sequence": ledger_sequence,
            }
            encoded = json.dumps(payload, separators=(",", ":")) + "\n"
            tmp_path = self.path.with_suffix(".tmp")
            with self._locked():
                tmp_path.write_text(encoded, encoding="utf-8")
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self.path)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to save cursor checkpoint %s: %s", self.path, exc)

    def delete(self) -> None:
        """Delete the checkpoint under the same advisory lock used by readers/writers."""
        try:
            with self._locked():
                self.path.unlink(missing_ok=True)
                self.path.with_suffix(".tmp").unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to delete cursor checkpoint %s: %s", self.path, exc)
