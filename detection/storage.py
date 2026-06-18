"""SQLite-backed persistence for `RiskScore` records and on-chain submission audit log.

`ledgerlens-api` will eventually own the canonical score store; until that
integration point is wired up (see README's "Open Integration Points"),
`run_pipeline.py` and the local API (`api/main.py`) persist and read
`RiskScore` records here.

## How to add a new migration
1. Append a tuple to `_MIGRATIONS`:
       (version, "short description", "ALTER TABLE ... or CREATE TABLE ...")
   where `version` is `len(_MIGRATIONS) + 1` before your append (i.e. next int).
2. The SQL is applied automatically the next time `init_db()` is called on an
   older database.  Each migration is applied exactly once and tracked in the
   `schema_migrations` log table.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config.settings import settings
from detection.risk_score import RiskScore


class SchemaMigrationError(RuntimeError):
    """Raised when a previously interrupted migration is detected on startup."""


# ---------------------------------------------------------------------------
# Migration registry
# Each entry: (version: int, description: str, sql: str)
# The SQL for version 1 must create every table that the rest of the module
# depends on.  Subsequent entries add incremental changes.
# ---------------------------------------------------------------------------
_MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
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

        CREATE TABLE IF NOT EXISTS pair_correlations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_a TEXT NOT NULL,
            pair_b TEXT NOT NULL,
            correlation_r REAL NOT NULL,
            method TEXT NOT NULL,
            shared_wallet_count INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pair_correlations_pair_a ON pair_correlations (pair_a);
        CREATE INDEX IF NOT EXISTS idx_pair_correlations_pair_b ON pair_correlations (pair_b);
        """,
    ),
    (
        2,
        "add shap_json column to risk_scores",
        "ALTER TABLE risk_scores ADD COLUMN shap_json TEXT;",
    ),
]


@contextmanager
def _connect(db_path: str | None = None):
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _ensure_meta_tables(conn: sqlite3.Connection) -> None:
    """Create the schema_version and schema_migrations tracking tables if absent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        """
    )
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version; 0 if the table is absent or empty."""
    try:
        rows = conn.execute("SELECT MAX(version) FROM schema_version", ()).fetchall()
        return rows[0][0] if rows and rows[0][0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def migrate_db(conn: sqlite3.Connection) -> list[int]:
    """Apply any pending migrations and return the list of versions applied.

    Uses an application-level state machine per migration:
    - Before running migration N, writes a log row with status='applying'.
    - On success, updates the row to status='applied'.
    - On failure, leaves the row as 'applying' and raises.
    On the *next* startup, if a row with status='applying' is detected,
    `SchemaMigrationError` is raised immediately so the operator can recover.

    Note: SQLite DDL statements (CREATE TABLE, ALTER TABLE) cannot be rolled
    back inside an explicit transaction — they auto-commit.  The state-machine
    approach here gives the next-best safety guarantee: the interrupted
    migration version is visible in the log so the operator knows exactly what
    happened and can either apply the remaining SQL manually or restore from a
    backup.
    """
    _ensure_meta_tables(conn)

    # Detect any previously interrupted migration.
    interrupted = conn.execute(
        "SELECT version FROM schema_migrations WHERE status = 'applying' ORDER BY version", ()
    ).fetchall()
    if interrupted:
        versions = [r[0] for r in interrupted]
        raise SchemaMigrationError(
            f"Migration(s) {versions} were interrupted during a previous run "
            f"(status='applying' in schema_migrations). "
            f"Recover by completing the migration SQL manually or restoring from a backup, "
            f"then update the status to 'applied' before restarting."
        )

    current = get_schema_version(conn)
    applied: list[int] = []

    for version, description, sql in _MIGRATIONS:
        if version <= current:
            continue

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO schema_migrations (version, description, status, started_at) VALUES (?, ?, 'applying', ?)",
            (version, description, now),
        )
        conn.commit()

        try:
            conn.executescript(sql)
            conn.commit()
        except Exception as exc:
            conn.execute(
                "UPDATE schema_migrations SET error = ? WHERE version = ? AND status = 'applying'",
                (str(exc), version),
            )
            conn.commit()
            raise

        finished = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE schema_migrations SET status = 'applied', finished_at = ? WHERE version = ? AND status = 'applying'",
            (finished, version),
        )
        # Upsert schema_version: keep a single row with the latest version.
        count_rows = conn.execute("SELECT COUNT(*) FROM schema_version", ()).fetchall()
        existing = count_rows[0][0] if count_rows else 0
        if existing == 0:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, finished),
            )
        else:
            conn.execute(
                "UPDATE schema_version SET version = ?, applied_at = ?",
                (version, finished),
            )
        conn.commit()
        applied.append(version)

    return applied


def init_db(db_path: str | None = None) -> None:
    """Initialise or upgrade the database schema via the migration system."""
    with _connect(db_path) as conn:
        migrate_db(conn)


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
    _, wallet, asset_pair, score, benford_flag, ml_flag, confidence, timestamp = row[:8]
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



def save_pair_correlations(
    correlations: list[tuple[str, str, float]],
    method: str,
    shared_wallet_counts: dict[tuple[str, str], int] | None = None,
    db_path: str | None = None,
) -> None:
    """Persist correlated pair results from the latest pipeline run."""
    if not correlations:
        return
    init_db(db_path)
    shared_wallet_counts = shared_wallet_counts or {}
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO pair_correlations
                (pair_a, pair_b, correlation_r, method, shared_wallet_count, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    pair_a,
                    pair_b,
                    correlation_r,
                    method,
                    shared_wallet_counts.get((pair_a, pair_b), 0),
                    ts,
                )
                for pair_a, pair_b, correlation_r in correlations
            ],
        )
        conn.commit()


def get_pair_correlations(db_path: str | None = None) -> list[dict]:
    """Return the most recent set of pair correlations."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT pc.pair_a, pc.pair_b, pc.correlation_r, pc.method,
                   pc.shared_wallet_count, pc.timestamp
            FROM pair_correlations pc
            JOIN (
                SELECT MAX(timestamp) AS max_ts FROM pair_correlations
            ) latest ON pc.timestamp = latest.max_ts
            ORDER BY pc.correlation_r DESC
            """
        ).fetchall()
    return [
        {
            "pair_a": row[0],
            "pair_b": row[1],
            "correlation_r": row[2],
            "method": row[3],
            "shared_wallet_count": row[4],
            "timestamp": row[5],
        }
        for row in rows
    ]


if __name__ == "__main__":
    init_db()
    print(f"Initialized risk score database at {settings.db_path}")
