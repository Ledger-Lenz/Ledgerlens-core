"""CSV and Parquet export endpoints for risk score data (Issue #163)."""

import io
import threading
from collections import defaultdict
from collections import deque
from datetime import datetime, timezone, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from fastapi import APIRouter, Depends, HTTPException, Query
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


def _query_rows(from_date: str, to_date: str, min_score: int, wallet: str | None) -> list[dict]:
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    if (to_dt - from_dt) > timedelta(days=_MAX_WINDOW_DAYS):
        raise HTTPException(status_code=400, detail=f"Export window cannot exceed {_MAX_WINDOW_DAYS} days")

    sql = (
        "SELECT id, wallet, asset_pair, score, benford_flag, ml_flag, confidence, timestamp "
        "FROM risk_scores WHERE timestamp >= ? AND timestamp < ? AND score >= ?"
    )
    params: list = [from_dt.isoformat(), to_dt.isoformat(), min_score]

    if wallet:
        sql += " AND wallet = ?"
        params.append(wallet)

    sql += " ORDER BY timestamp DESC"

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [dict(zip(_COLUMNS, row)) for row in rows]


def _filename(fmt: str, from_date: str, to_date: str) -> str:
    return f"ledgerlens_scores_{from_date}_{to_date}.{fmt}"


@router.get("/scores.csv", include_in_schema=True, dependencies=[Depends(require_admin_key)])
def export_csv(
    from_date: str = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="End date YYYY-MM-DD"),
    min_score: int = Query(default=0, ge=0, le=100),
    wallet: str | None = Query(default=None),
    x_ledgerlens_admin_key: str = Query(default="", include_in_schema=False),
) -> StreamingResponse:
    """Stream risk scores as CSV. Max 90-day window. Requires admin key."""
    _check_rate_limit(x_ledgerlens_admin_key or "")
    rows = _query_rows(from_date, to_date, min_score, wallet)

    buf = io.StringIO()
    buf.write(",".join(_COLUMNS) + "\n")
    for row in rows:
        buf.write(",".join(str(row[c]) for c in _COLUMNS) + "\n")
    buf.seek(0)

    filename = _filename("csv", from_date, to_date)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/scores.parquet", include_in_schema=True, dependencies=[Depends(require_admin_key)])
def export_parquet(
    from_date: str = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="End date YYYY-MM-DD"),
    min_score: int = Query(default=0, ge=0, le=100),
    wallet: str | None = Query(default=None),
    x_ledgerlens_admin_key: str = Query(default="", include_in_schema=False),
) -> StreamingResponse:
    """Stream risk scores as Parquet (snappy compressed). Max 90-day window. Requires admin key."""
    _check_rate_limit(x_ledgerlens_admin_key or "")
    rows = _query_rows(from_date, to_date, min_score, wallet)

    if rows:
        table = pa.Table.from_pylist(rows)
    else:
        schema = pa.schema([
            ("id", pa.int64()), ("wallet", pa.string()), ("asset_pair", pa.string()),
            ("score", pa.int64()), ("benford_flag", pa.int64()), ("ml_flag", pa.int64()),
            ("confidence", pa.int64()), ("timestamp", pa.string()),
        ])
        table = pa.table({col: pa.array([], type=schema.field(col).type) for col in _COLUMNS})

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)

    filename = _filename("parquet", from_date, to_date)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
