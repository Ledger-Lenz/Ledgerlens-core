"""CSV and Parquet export endpoints for risk score data (Issue #163)."""

import io
import threading
from collections import defaultdict
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from api.auth import require_admin_key
from config.settings import settings
from detection.storage import _connect

router = APIRouter(prefix="/export", tags=["export"])

# ---------------------------------------------------------------------------
# Rate limiting: 10 exports/hour per admin key
# ---------------------------------------------------------------------------

_rate_limit_lock = threading.Lock()
_rate_limit_store: dict[str, deque] = defaultdict(deque)
_RATE_LIMIT = 10
_RATE_WINDOW_SECONDS = 3600

_COLUMNS = ["id", "wallet", "asset_pair", "score", "benford_flag", "ml_flag", "confidence", "timestamp"]
_MAX_WINDOW_DAYS = 90
_PARQUET_SCHEMA = pa.schema([
    ("id", pa.int64()), ("wallet", pa.string()), ("asset_pair", pa.string()),
    ("score", pa.int64()), ("benford_flag", pa.int64()), ("ml_flag", pa.int64()),
    ("confidence", pa.int64()), ("timestamp", pa.string()),
])


def _check_rate_limit(admin_key: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - _RATE_WINDOW_SECONDS
    with _rate_limit_lock:
        dq = _rate_limit_store[admin_key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded: 10 exports per hour")
        dq.append(now)


def _build_query(from_date: str, to_date: str, min_score: int, wallet: str | None) -> tuple[str, list]:
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    if (to_dt - from_dt) > timedelta(days=_MAX_WINDOW_DAYS):
        raise HTTPException(status_code=400, detail=f"Export window cannot exceed {_MAX_WINDOW_DAYS} days")

    where = "FROM risk_scores WHERE timestamp >= ? AND timestamp < ? AND score >= ?"
    params: list = [from_dt.isoformat(), to_dt.isoformat(), min_score]

    if wallet:
        where += " AND wallet = ?"
        params.append(wallet)

    return where, params


def _prepare_export(from_date: str, to_date: str, min_score: int, wallet: str | None) -> tuple[str, list]:
    """Validate the request and enforce the row cap before any bytes are sent.

    Returns the SELECT statement to stream. Raises 413 when the result set
    exceeds ``settings.export_max_rows`` so a single export can never pin an
    unbounded amount of server memory or DB cursor time.
    """
    where, params = _build_query(from_date, to_date, min_score, wallet)
    with _connect() as conn:
        (count,) = conn.execute(f"SELECT COUNT(*) {where}", params).fetchone()
    if count > settings.export_max_rows:
        raise HTTPException(
            status_code=413,
            detail=f"Export exceeds {settings.export_max_rows} rows; narrow the date range or filters",
        )
    sql = f"SELECT {', '.join(_COLUMNS)} {where} ORDER BY timestamp DESC"
    return sql, params


def _iter_row_chunks(sql: str, params: list) -> Iterator[list[dict]]:
    """Yield result rows in chunks of ``settings.export_chunk_size``.

    Only one chunk is held in memory at a time.
    """
    chunk_size = max(1, settings.export_chunk_size)
    with _connect() as conn:
        cursor = conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            yield [dict(zip(_COLUMNS, row)) for row in rows]


def _csv_stream(sql: str, params: list) -> Iterator[str]:
    yield ",".join(_COLUMNS) + "\n"
    for chunk in _iter_row_chunks(sql, params):
        yield "".join(",".join(str(row[c]) for c in _COLUMNS) + "\n" for row in chunk)


def _parquet_stream(sql: str, params: list) -> Iterator[bytes]:
    """Write one Parquet row group per chunk, flushing bytes as they are produced."""
    buf = io.BytesIO()
    writer = pq.ParquetWriter(buf, _PARQUET_SCHEMA, compression="snappy")
    try:
        for chunk in _iter_row_chunks(sql, params):
            writer.write_table(pa.Table.from_pylist(chunk, schema=_PARQUET_SCHEMA))
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    finally:
        writer.close()
    yield buf.getvalue()


def _filename(fmt: str, from_date: str, to_date: str) -> str:
    return f"ledgerlens_scores_{from_date}_{to_date}.{fmt}"


@router.get("/scores.csv", include_in_schema=True, dependencies=[Depends(require_admin_key)])
def export_csv(
    from_date: str = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="End date YYYY-MM-DD"),
    min_score: int = Query(default=0, ge=0, le=100),
    wallet: str | None = Query(default=None),
    x_ledgerlens_admin_key: str = Header(default="", include_in_schema=False),
) -> StreamingResponse:
    """Stream risk scores as CSV. Max 90-day window. Requires admin key."""
    _check_rate_limit(x_ledgerlens_admin_key or "")
    sql, params = _prepare_export(from_date, to_date, min_score, wallet)

    filename = _filename("csv", from_date, to_date)
    return StreamingResponse(
        _csv_stream(sql, params),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/scores.parquet", include_in_schema=True, dependencies=[Depends(require_admin_key)])
def export_parquet(
    from_date: str = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="End date YYYY-MM-DD"),
    min_score: int = Query(default=0, ge=0, le=100),
    wallet: str | None = Query(default=None),
    x_ledgerlens_admin_key: str = Header(default="", include_in_schema=False),
) -> StreamingResponse:
    """Stream risk scores as Parquet (snappy compressed). Max 90-day window. Requires admin key."""
    _check_rate_limit(x_ledgerlens_admin_key or "")
    sql, params = _prepare_export(from_date, to_date, min_score, wallet)

    filename = _filename("parquet", from_date, to_date)
    return StreamingResponse(
        _parquet_stream(sql, params),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
