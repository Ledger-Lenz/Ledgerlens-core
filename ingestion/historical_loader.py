"""Sequential and parallel historical trade ingestion from Stellar Horizon."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
import pandas as pd

from config.settings import settings
from detection.storage import RiskScoreStore
from ingestion.horizon_streamer import _parse_trade
from ingestion.http_client import (
    AsyncHorizonClient,
    RetryingHorizonClient,
    get_with_retry,
)

PAGE_LIMIT = 200
logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def partition_range(
    start: datetime,
    end: datetime,
    chunk_hours: float = 6.0,
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end]`` into non-overlapping half-open chunks."""
    start, end = _utc(start), _utc(end)
    if start >= end:
        raise ValueError("start must be before end")
    if chunk_hours <= 0:
        raise ValueError("chunk_hours must be positive")

    step = timedelta(hours=chunk_hours)
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + step, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


@dataclass
class ChunkProgress:
    chunk_id: str
    start: datetime
    end: datetime
    status: Literal["pending", "in_progress", "complete", "failed"]
    records_fetched: int = 0
    completed_at: datetime | None = None
    error: str | None = None


class ProgressTracker:
    """Persist restart-safe chunk state to an atomically replaced JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.progress = self.load()

    def load(self) -> dict[str, ChunkProgress]:
        """Load progress; malformed or unreadable files are treated as empty."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            loaded: dict[str, ChunkProgress] = {}
            valid_statuses = {"pending", "in_progress", "complete", "failed"}
            for chunk_id, item in raw.items():
                if not isinstance(item, dict) or item.get("status") not in valid_statuses:
                    return {}
                loaded[chunk_id] = ChunkProgress(
                    chunk_id=chunk_id,
                    start=datetime.fromisoformat(item["start"]),
                    end=datetime.fromisoformat(item["end"]),
                    status=item["status"],
                    records_fetched=int(item.get("records_fetched", 0)),
                    completed_at=(
                        datetime.fromisoformat(item["completed_at"])
                        if item.get("completed_at")
                        else None
                    ),
                    error=item.get("error"),
                )
            return loaded
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}

    def register(self, progress: ChunkProgress) -> None:
        if progress.chunk_id not in self.progress:
            self.progress[progress.chunk_id] = progress
            self.save()

    def mark_in_progress(self, chunk_id: str) -> None:
        item = self.progress[chunk_id]
        item.status = "in_progress"
        item.error = None
        self.save()

    def mark_complete(self, chunk_id: str, records_fetched: int) -> None:
        item = self.progress[chunk_id]
        item.status = "complete"
        item.records_fetched = records_fetched
        item.completed_at = datetime.now(timezone.utc)
        item.error = None
        self.save()

    def mark_failed(self, chunk_id: str, error: str) -> None:
        item = self.progress[chunk_id]
        item.status = "failed"
        item.error = error
        self.save()

    def save(self) -> None:
        """Atomically save current progress without exposing a partial file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {}
        for chunk_id, item in self.progress.items():
            data = asdict(item)
            data["start"] = item.start.isoformat()
            data["end"] = item.end.isoformat()
            data["completed_at"] = (
                item.completed_at.isoformat() if item.completed_at else None
            )
            payload[chunk_id] = data
        temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)


@dataclass
class ChunkResult:
    chunk: tuple[datetime, datetime]
    records_fetched: int


@dataclass
class LoadResult:
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    skipped_chunks: int
    total_records: int
    duration_seconds: float
    records_per_second: float


class ParallelHistoricalLoader:
    """Fetch independent historical time chunks concurrently and resumably."""

    def __init__(
        self,
        client: RetryingHorizonClient,
        storage: RiskScoreStore,
        concurrency: int = 4,
        chunk_hours: float = 6.0,
        progress_path: Path = Path("./data/historical_progress.json"),
    ) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if chunk_hours <= 0:
            raise ValueError("chunk_hours must be positive")
        self.client = client
        self.storage = storage
        self.concurrency = concurrency
        self.chunk_hours = chunk_hours
        self.progress_path = _resolve_progress_path(progress_path)
        self.tracker = ProgressTracker(self.progress_path)

    async def load(
        self,
        start: datetime,
        end: datetime,
        asset_pair: str | None = None,
        resume: bool = True,
    ) -> LoadResult:
        """Load all chunks, continuing past failed workers and summarizing results."""
        started = time.perf_counter()
        start, end = _utc(start), _utc(end)
        _validate_load_range(start, end)
        chunks = partition_range(start, end, self.chunk_hours)
        sem = asyncio.Semaphore(self.concurrency)
        pending: list[tuple[str, tuple[datetime, datetime]]] = []
        skipped = 0
        skipped_records = 0
        progress_changed = False

        for chunk in chunks:
            chunk_id = _chunk_id(chunk, asset_pair)
            prior = self.tracker.progress.get(chunk_id)
            if resume and prior and prior.status == "complete":
                skipped += 1
                skipped_records += prior.records_fetched
                continue
            if chunk_id not in self.tracker.progress:
                self.tracker.progress[chunk_id] = ChunkProgress(
                    chunk_id=chunk_id,
                    start=chunk[0],
                    end=chunk[1],
                    status="pending",
                )
                progress_changed = True
            pending.append((chunk_id, chunk))
        if progress_changed:
            self.tracker.save()

        async def run_one(
            chunk_id: str,
            chunk: tuple[datetime, datetime],
        ) -> ChunkResult:
            self.tracker.mark_in_progress(chunk_id)
            try:
                result = await self._fetch_chunk(chunk, asset_pair, sem)
            except Exception as exc:
                self.tracker.mark_failed(chunk_id, str(exc))
                raise
            self.tracker.mark_complete(chunk_id, result.records_fetched)
            return result

        tasks = [
            asyncio.create_task(run_one(chunk_id, chunk))
            for chunk_id, chunk in pending
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        successful = [item for item in outcomes if isinstance(item, ChunkResult)]
        failed = len(outcomes) - len(successful)
        total_records = skipped_records + sum(item.records_fetched for item in successful)
        duration = time.perf_counter() - started
        result = LoadResult(
            total_chunks=len(chunks),
            completed_chunks=len(successful),
            failed_chunks=failed,
            skipped_chunks=skipped,
            total_records=total_records,
            duration_seconds=duration,
            records_per_second=total_records / duration if duration else 0.0,
        )
        logger.info("Historical load complete: %s", result)
        return result

    async def _fetch_chunk(
        self,
        chunk: tuple[datetime, datetime],
        asset_pair: str | None,
        sem: asyncio.Semaphore,
    ) -> ChunkResult:
        """Fetch and validate every page in one half-open chunk, writing per page."""
        start, end = chunk
        params: dict[str, str | int] = {
            "limit": PAGE_LIMIT,
            "order": "asc",
            "start_time": _horizon_time(start),
            "end_time": _horizon_time(end),
        }
        if asset_pair:
            base, counter = _parse_asset_pair(asset_pair)
            params.update(_asset_params("base", base))
            params.update(_asset_params("counter", counter))

        path: str | None = "/trades"
        request_params: dict | None = params
        records_fetched = 0
        async with sem:
            while path:
                payload = await self.client.get(path, params=request_params)
                raw_records = payload.get("_embedded", {}).get("records", [])
                if not raw_records:
                    break

                validated = []
                reached_end = False
                for raw in raw_records:
                    trade = _parse_trade(raw)
                    close_time = _utc(trade.ledger_close_time)
                    if close_time >= end:
                        reached_end = True
                        continue
                    if close_time >= start:
                        validated.append(trade)

                if validated:
                    await asyncio.to_thread(self.storage.upsert_trades, validated)
                    records_fetched += len(validated)
                if reached_end:
                    break

                path = payload.get("_links", {}).get("next", {}).get("href") or None
                request_params = None

        return ChunkResult(chunk=chunk, records_fetched=records_fetched)


def _resolve_progress_path(path: Path) -> Path:
    data_root = Path(settings.data_dir).expanduser().resolve()
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError as exc:
        raise ValueError("progress_path must remain inside DATA_DIR") from exc
    return candidate


def _validate_load_range(start: datetime, end: datetime) -> None:
    if start >= end:
        raise ValueError("start must be before end")
    maximum = timedelta(days=settings.historical_max_lookback_days)
    if end - start > maximum:
        raise ValueError(
            f"historical load range exceeds maximum of "
            f"{settings.historical_max_lookback_days} days"
        )


def _chunk_id(chunk: tuple[datetime, datetime], asset_pair: str | None) -> str:
    value = f"{chunk[0].isoformat()}|{chunk[1].isoformat()}|{asset_pair or ''}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _horizon_time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_asset_pair(asset_pair: str) -> tuple[str, str]:
    try:
        base, counter = asset_pair.split("/", 1)
    except ValueError as exc:
        raise ValueError("asset_pair must be BASE/COUNTER") from exc
    if not base or not counter:
        raise ValueError("asset_pair must be BASE/COUNTER")
    return base, counter


def load_historical_trades(
    base_asset: str | None = None,
    counter_asset: str | None = None,
    lookback_days: int | None = None,
) -> pd.DataFrame:
    """Fetch historical trades sequentially and return a DataFrame."""
    lookback_days = lookback_days or settings.trade_history_lookback_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    params: dict[str, str | int] = {"limit": PAGE_LIMIT, "order": "desc"}
    if base_asset:
        params.update(_asset_params("base", base_asset))
    if counter_asset:
        params.update(_asset_params("counter", counter_asset))

    records: list[dict] = []
    url = f"{settings.horizon_url}/trades"
    with httpx.Client(timeout=30.0) as client:
        while url:
            response = get_with_retry(client, url, params=params)
            payload = response.json()
            page_records = payload["_embedded"]["records"]
            if not page_records:
                break
            for record in page_records:
                close_time = datetime.fromisoformat(
                    record["ledger_close_time"].replace("Z", "+00:00")
                )
                if close_time < cutoff:
                    return _to_dataframe(records)
                records.append(record)
            url = payload["_links"]["next"]["href"]
            params = {}
    return _to_dataframe(records)


def _asset_params(prefix: str, asset: str) -> dict[str, str]:
    if ":" in asset:
        code, issuer = asset.split(":", 1)
        asset_type = "credit_alphanum12" if len(code) > 4 else "credit_alphanum4"
        return {
            f"{prefix}_asset_type": asset_type,
            f"{prefix}_asset_code": code,
            f"{prefix}_asset_issuer": issuer,
        }
    return {f"{prefix}_asset_type": "native"}


def _to_dataframe(records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([_parse_trade(record).model_dump() for record in records])


async def async_load_historical_trades(
    base_asset: str | None = None,
    counter_asset: str | None = None,
    lookback_days: int | None = None,
    client: AsyncHorizonClient | None = None,
) -> pd.DataFrame:
    """Fetch historical trades sequentially using the shared async client."""
    assert client is not None, "client is required for async_load_historical_trades"
    lookback_days = lookback_days or settings.trade_history_lookback_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    params: dict[str, str | int] = {"limit": PAGE_LIMIT, "order": "desc"}
    if base_asset:
        params.update(_asset_params("base", base_asset))
    if counter_asset:
        params.update(_asset_params("counter", counter_asset))

    records: list[dict] = []
    path: str | None = "/trades"
    request_params: dict | None = params
    while path:
        payload = await client.get(path, params=request_params)
        page_records = payload.get("_embedded", {}).get("records", [])
        if not page_records:
            break
        for record in page_records:
            close_time = datetime.fromisoformat(
                record["ledger_close_time"].replace("Z", "+00:00")
            )
            if close_time < cutoff:
                return _to_dataframe(records)
            records.append(record)
        path = payload.get("_links", {}).get("next", {}).get("href") or None
        request_params = None
    return _to_dataframe(records)
