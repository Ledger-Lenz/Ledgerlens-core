"""SQLite-backed persistence for `RiskScore` records and on-chain submission audit log.

`ledgerlens-api` will eventually own the canonical score store; until that
integration point is wired up (see README's "Open Integration Points"),
`run_pipeline.py` and the local API (`api/main.py`) persist and read
`RiskScore` records here.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config.settings import settings
from detection.risk_score import RiskScore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    asset_pair TEXT NOT NULL,
    score INTEGER NOT NULL,
    benford_flag INTEGER NOT NULL,
    ml_flag INTEGER NOT NULL,
    confidence INTEGER NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_scores_wallet ON risk_scores (wallet);
CREATE INDEX IF NOT EXISTS idx_risk_scores_asset_pair ON risk_scores (asset_pair);

CREATE TABLE IF NOT EXISTS on_chain_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    asset_pair TEXT NOT NULL,
    score INTEGER NOT NULL,
    tx_hash TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_submissions_wallet ON on_chain_submissions (wallet);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON on_chain_submissions (status);
"""


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """Create the `risk_scores` table if it does not already exist."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def save_scores(scores: list[RiskScore], db_path: str | None = None) -> None:
    """Insert `scores` into the store, creating the schema first if needed."""
    if not scores:
        return
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO risk_scores
                (wallet, asset_pair, score, benford_flag, ml_flag, confidence, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.wallet,
                    s.asset_pair,
                    s.score,
                    int(s.benford_flag),
                    int(s.ml_flag),
                    s.confidence,
                    s.timestamp.isoformat(),
                )
                for s in scores
            ],
        )
        conn.commit()


def save_submission(
    wallet: str,
    asset_pair: str,
    score: int,
    status: str,
    tx_hash: str | None = None,
    error_message: str | None = None,
    db_path: str | None = None,
) -> None:
    """Insert a row into the ``on_chain_submissions`` audit table."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO on_chain_submissions
                (wallet, asset_pair, score, tx_hash, status, error_message, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                asset_pair,
                score,
                tx_hash,
                status,
                error_message,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def _row_to_score(row: tuple) -> RiskScore:
    _, wallet, asset_pair, score, benford_flag, ml_flag, confidence, timestamp = row
    return RiskScore(
        wallet=wallet,
        asset_pair=asset_pair,
        score=score,
        benford_flag=bool(benford_flag),
        ml_flag=bool(ml_flag),
        confidence=confidence,
        timestamp=datetime.fromisoformat(timestamp),
    )


def get_latest_scores(
    wallet: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    db_path: str | None = None,
    benford_flag: bool | None = None,
    ml_flag: bool | None = None,
    sort_by: str = "score",
) -> list[RiskScore]:
    """Return the most recent score for each (wallet, asset_pair) pair.

    If `wallet` is given, restrict to that wallet. Optional flag filters are
    applied to the latest rows in SQLite, ordered by `sort_by` descending.
    Paging is done in SQL (via LIMIT/OFFSET), not Python.
    """
    sort_columns = {
        "score": "rs.score",
        "confidence": "rs.confidence",
        "timestamp": "rs.timestamp",
    }
    if sort_by not in sort_columns:
        raise ValueError("sort_by must be one of: score, confidence, timestamp")

    init_db(db_path)

    query = """
        SELECT rs.* FROM risk_scores rs
        JOIN (
            SELECT wallet, asset_pair, MAX(timestamp) AS max_ts
            FROM risk_scores
            {where}
            GROUP BY wallet, asset_pair
        ) latest
        ON rs.wallet = latest.wallet
        AND rs.asset_pair = latest.asset_pair
        AND rs.timestamp = latest.max_ts
        {outer_where}
        ORDER BY {order_by} DESC
        {limit_offset}
    """
    params: list = []
    where = ""
    if wallet is not None:
        where = "WHERE wallet = ?"
        params.append(wallet)

    outer_conditions = []
    if benford_flag is not None:
        outer_conditions.append("rs.benford_flag = ?")
        params.append(int(benford_flag))
    if ml_flag is not None:
        outer_conditions.append("rs.ml_flag = ?")
        params.append(int(ml_flag))
    outer_where = ""
    if outer_conditions:
        outer_where = "WHERE " + " AND ".join(outer_conditions)

    limit_offset = ""
    if limit is not None:
        limit_offset = "LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    with _connect(db_path) as conn:
        rows = conn.execute(
            query.format(
                where=where,
                outer_where=outer_where,
                order_by=sort_columns[sort_by],
                limit_offset=limit_offset,
            ),
            tuple(params),
        ).fetchall()

    return [_row_to_score(row) for row in rows]



if __name__ == "__main__":
    init_db()
    print(f"Initialized risk score database at {settings.db_path}")
