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

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from config.settings import settings
from detection.risk_score import RiskScore
from ingestion.data_models import PathPayment


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
    (
        3,
        "add feature_vectors table with shap cache",
        """
        CREATE TABLE IF NOT EXISTS feature_vectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            asset_pair TEXT NOT NULL,
            features_json TEXT NOT NULL,
            shap_json TEXT,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_feature_vectors_wallet ON feature_vectors (wallet);
        CREATE INDEX IF NOT EXISTS idx_feature_vectors_asset_pair ON feature_vectors (asset_pair);
        """,
    ),
    (
        4,
        "add AMM pool trade, path payment, and circular route tables",
        """
        CREATE TABLE IF NOT EXISTS liquidity_pool_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL,
            pool_id TEXT NOT NULL,
            base_account TEXT NOT NULL,
            base_asset_pair TEXT NOT NULL,
            counter_asset_pair TEXT NOT NULL,
            base_amount REAL NOT NULL,
            counter_amount REAL NOT NULL,
            base_is_seller INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lp_trades_pool_id ON liquidity_pool_trades (pool_id);
        CREATE INDEX IF NOT EXISTS idx_lp_trades_base_account ON liquidity_pool_trades (base_account);

        CREATE TABLE IF NOT EXISTS path_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL,
            transaction_hash TEXT NOT NULL,
            source_account TEXT NOT NULL,
            destination_account TEXT NOT NULL,
            source_asset_pair TEXT NOT NULL,
            destination_asset_pair TEXT NOT NULL,
            source_amount REAL NOT NULL,
            destination_amount REAL NOT NULL,
            hop_count INTEGER NOT NULL,
            strict_send INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_path_payments_source ON path_payments (source_account);
        CREATE INDEX IF NOT EXISTS idx_path_payments_tx_hash ON path_payments (transaction_hash);

        CREATE TABLE IF NOT EXISTS circular_path_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_hash TEXT NOT NULL,
            accounts_json TEXT NOT NULL,
            hop_count INTEGER NOT NULL,
            cycle_volume REAL NOT NULL,
            is_atomic_self_payment INTEGER NOT NULL,
            touches_pool INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_circular_routes_tx_hash ON circular_path_routes (transaction_hash);
        """,
    ),
    (
        5,
        "add drift_reports and retrain_runs tables for model governance",
        """
        CREATE TABLE IF NOT EXISTS drift_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at TEXT NOT NULL,
            drift_detected INTEGER NOT NULL,
            psi_report_json TEXT NOT NULL,
            psi_threshold REAL NOT NULL,
            min_drifted_features INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_drift_reports_triggered_at ON drift_reports (triggered_at);

        CREATE TABLE IF NOT EXISTS retrain_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at TEXT NOT NULL,
            drift_report_id INTEGER REFERENCES drift_reports(id),
            model_name TEXT NOT NULL,
            old_version TEXT,
            new_version TEXT,
            old_auc_roc REAL,
            new_auc_roc REAL,
            promoted INTEGER NOT NULL,
            forced INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_retrain_runs_triggered_at ON retrain_runs (triggered_at);
        CREATE INDEX IF NOT EXISTS idx_retrain_runs_model_name ON retrain_runs (model_name);
        """,
    ),
    (
        6,
        "add robustness_reports table for adversarial evaluation",
        """
        CREATE TABLE IF NOT EXISTS robustness_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            asr_json TEXT NOT NULL,
            mean_map REAL NOT NULL,
            p95_map REAL NOT NULL,
            certified_radius REAL NOT NULL,
            n_samples INTEGER NOT NULL,
            epsilon REAL NOT NULL,
            report_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_robustness_reports_created_at ON robustness_reports (created_at);
        """,
    ),
    (
        7,
        "add dispute, committee, overrides, runtime_config and governance tables",
        """
        CREATE TABLE IF NOT EXISTS committee_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_key_hex TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_committee_key_hash ON committee_members (key_hash);

        CREATE TABLE IF NOT EXISTS score_disputes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispute_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            asset_pair TEXT NOT NULL,
            disputed_score INTEGER NOT NULL,
            soroban_tx_hash TEXT NOT NULL,
            evidence_url TEXT,
            submitted_at TEXT NOT NULL,
            status TEXT NOT NULL,
            committee_votes_json TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_score_disputes_dispute_id ON score_disputes (dispute_id);
        CREATE INDEX IF NOT EXISTS idx_score_disputes_wallet ON score_disputes (wallet);

        CREATE TABLE IF NOT EXISTS score_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            asset_pair TEXT NOT NULL,
            dispute_id TEXT NOT NULL,
            tx_hash TEXT,
            status TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_score_overrides_dispute_id ON score_overrides (dispute_id);

        CREATE TABLE IF NOT EXISTS runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS governance_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL,
            proposal_type TEXT NOT NULL,
            proposed_value TEXT NOT NULL,
            proposed_by_key_hash TEXT NOT NULL,
            votes_for_json TEXT NOT NULL,
            votes_against_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_governance_proposals_proposal_id ON governance_proposals (proposal_id);
        """,
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


def save_feature_vectors(vectors: list[dict], db_path: str | None = None) -> None:
    """Persist a list of feature vector dicts to the ``feature_vectors`` table.

    Each dict must contain ``wallet``, ``asset_pair``, and ``features``
    (the raw feature dict produced by ``build_feature_vector``).
    """
    if not vectors:
        return
    init_db(db_path)
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO feature_vectors (wallet, asset_pair, features_json, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    v["wallet"],
                    v["asset_pair"],
                    json.dumps(v["features"]),
                    ts,
                )
                for v in vectors
            ],
        )
        conn.commit()


def get_feature_vector(
    wallet: str,
    asset_pair: str,
    db_path: str | None = None,
) -> dict | None:
    """Return the most recent feature dict for ``wallet`` / ``asset_pair``, or ``None``."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT features_json FROM feature_vectors
            WHERE wallet = ? AND asset_pair = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (wallet, asset_pair),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def save_shap_values(
    wallet: str,
    asset_pair: str,
    shap_values: list[dict],
    db_path: str | None = None,
) -> None:
    """Persist SHAP values for the most recent ``feature_vectors`` row for the pair.

    ``shap_values`` must be a list of ``{"feature": str, "shap_value": float}``
    dicts ordered by absolute contribution descending (top-5).
    """
    init_db(db_path)
    shap_json = json.dumps(shap_values)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE feature_vectors SET shap_json = ?
            WHERE id = (
                SELECT id FROM feature_vectors
                WHERE wallet = ? AND asset_pair = ?
                ORDER BY timestamp DESC
                LIMIT 1
            )
            """,
            (shap_json, wallet, asset_pair),
        )
        conn.commit()


def get_shap_values(
    wallet: str,
    asset_pair: str,
    db_path: str | None = None,
) -> list[dict] | None:
    """Return the cached SHAP values for ``wallet`` / ``asset_pair``, or ``None``.

    Returns a list of ``{"feature": str, "shap_value": float}`` dicts ordered
    by absolute contribution descending, or ``None`` if no cache entry exists.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT shap_json FROM feature_vectors
            WHERE wallet = ? AND asset_pair = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (wallet, asset_pair),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return json.loads(row[0])


def save_drift_report(
    drift_detected: bool,
    psi_report: dict,
    psi_threshold: float,
    min_drifted_features: int,
    db_path: str | None = None,
) -> int:
    """Persist a drift report; returns its row id (used as retrain_runs.drift_report_id)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO drift_reports
                (triggered_at, drift_detected, psi_report_json, psi_threshold, min_drifted_features)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                int(drift_detected),
                json.dumps(psi_report),
                psi_threshold,
                min_drifted_features,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def _row_to_drift_report(row: tuple) -> dict:
    return {
        "id": row[0],
        "triggered_at": row[1],
        "drift_detected": bool(row[2]),
        "psi_report": json.loads(row[3]),
        "psi_threshold": row[4],
        "min_drifted_features": row[5],
    }


def get_drift_reports(limit: int = 50, db_path: str | None = None) -> list[dict]:
    """Most recent drift reports first."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, triggered_at, drift_detected, psi_report_json, psi_threshold, min_drifted_features
            FROM drift_reports
            ORDER BY triggered_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_drift_report(row) for row in rows]


def save_retrain_run(
    drift_report_id: int | None,
    model_name: str,
    old_version: str | None,
    new_version: str | None,
    old_auc_roc: float | None,
    new_auc_roc: float | None,
    promoted: bool,
    forced: bool,
    db_path: str | None = None,
) -> None:
    """Persist a single model's retrain outcome for one retrain-check run."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO retrain_runs
                (triggered_at, drift_report_id, model_name, old_version, new_version,
                 old_auc_roc, new_auc_roc, promoted, forced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                drift_report_id,
                model_name,
                old_version,
                new_version,
                old_auc_roc,
                new_auc_roc,
                int(promoted),
                int(forced),
            ),
        )
        conn.commit()


def _row_to_retrain_run(row: tuple) -> dict:
    return {
        "id": row[0],
        "triggered_at": row[1],
        "drift_report_id": row[2],
        "model_name": row[3],
        "old_version": row[4],
        "new_version": row[5],
        "old_auc_roc": row[6],
        "new_auc_roc": row[7],
        "promoted": bool(row[8]),
        "forced": bool(row[9]),
    }


def get_retrain_runs(
    limit: int = 50,
    model_name: str | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """Most recent retrain runs first, optionally filtered by ``model_name``."""
    init_db(db_path)
    where = ""
    params: list = []
    if model_name is not None:
        where = "WHERE model_name = ?"
        params.append(model_name)
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, triggered_at, drift_report_id, model_name, old_version, new_version,
                   old_auc_roc, new_auc_roc, promoted, forced
            FROM retrain_runs
            {where}
            ORDER BY triggered_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_retrain_run(row) for row in rows]


def save_robustness_report(report: dict, db_path: str | None = None) -> None:
    """Persist a robustness report dict to the robustness_reports table."""
    init_db(db_path)
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO robustness_reports
                (created_at, model_version, asr_json, mean_map, p95_map, certified_radius, n_samples, epsilon, report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                report.get("model_version", ""),
                json.dumps(report.get("asr", {})),
                float(report.get("mean_map", 0.0)),
                float(report.get("p95_map", 0.0)),
                float(report.get("certified_radius", 0.0)),
                int(report.get("n_samples", 0)),
                float(report.get("epsilon", 0.0)),
                json.dumps(report),
            ),
        )
        conn.commit()


def get_latest_robustness_report(db_path: str | None = None) -> dict | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT report_json FROM robustness_reports ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _asset_pair_symbol(asset: dict) -> str:
    code = asset["code"]
    issuer = asset.get("issuer")
    return code if issuer is None else f"{code}:{issuer}"


def save_liquidity_pool_trades(pool_trades: pd.DataFrame, db_path: str | None = None) -> None:
    """Persist AMM pool trades (`trade_type == LIQUIDITY_POOL`) from the latest pipeline run.

    `pool_trades` is a `Trade`-shaped DataFrame (as built from `Trade.model_dump()`
    records) already filtered to pool trades.
    """
    if pool_trades.empty:
        return
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO liquidity_pool_trades
                (trade_id, pool_id, base_account, base_asset_pair, counter_asset_pair,
                 base_amount, counter_amount, base_is_seller, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["id"],
                    row["liquidity_pool_id"],
                    row["base_account"],
                    _asset_pair_symbol(row["base_asset"]),
                    _asset_pair_symbol(row["counter_asset"]),
                    row["base_amount"],
                    row["counter_amount"],
                    int(row["base_is_seller"]),
                    pd.Timestamp(row["ledger_close_time"]).isoformat(),
                )
                for _, row in pool_trades.iterrows()
            ],
        )
        conn.commit()


def get_liquidity_pool_trades(
    pool_id: str,
    limit: int | None = None,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    """Return stored trades against `pool_id`, most recent first, paginated."""
    init_db(db_path)
    query = """
        SELECT base_account, base_asset_pair, counter_asset_pair, base_amount,
               counter_amount, base_is_seller, timestamp
        FROM liquidity_pool_trades
        WHERE pool_id = ?
        ORDER BY timestamp DESC
    """
    params: list = [pool_id]
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    return [
        {
            "base_account": row[0],
            "base_asset_pair": row[1],
            "counter_asset_pair": row[2],
            "base_amount": row[3],
            "counter_amount": row[4],
            "base_is_seller": bool(row[5]),
            "timestamp": row[6],
        }
        for row in rows
    ]


def save_path_payments(payments: list[PathPayment], db_path: str | None = None) -> None:
    """Persist raw ingested path payments."""
    if not payments:
        return
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO path_payments
                (payment_id, transaction_hash, source_account, destination_account,
                 source_asset_pair, destination_asset_pair, source_amount, destination_amount,
                 hop_count, strict_send, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p.id,
                    p.transaction_hash,
                    p.source_account,
                    p.destination_account,
                    p.source_asset.pair_symbol,
                    p.destination_asset.pair_symbol,
                    p.source_amount,
                    p.destination_amount,
                    len(p.path) + 1,
                    int(p.strict_send),
                    p.timestamp.isoformat(),
                )
                for p in payments
            ],
        )
        conn.commit()


def save_circular_routes(routes: list[dict], db_path: str | None = None) -> None:
    """Persist `detect_atomic_circular_routes` output from the latest pipeline run."""
    if not routes:
        return
    init_db(db_path)
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO circular_path_routes
                (transaction_hash, accounts_json, hop_count, cycle_volume,
                 is_atomic_self_payment, touches_pool, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["transaction_hash"],
                    json.dumps(r["accounts"]),
                    r["hop_count"],
                    r["cycle_volume"],
                    int(r["is_atomic_self_payment"]),
                    int(r["touches_pool"]),
                    ts,
                )
                for r in routes
            ],
        )
        conn.commit()


def get_circular_routes(
    limit: int | None = None,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    """Return detected circular path-payment routes, most recent first, paginated."""
    init_db(db_path)
    query = "SELECT transaction_hash, accounts_json, hop_count, cycle_volume, is_atomic_self_payment, touches_pool, timestamp FROM circular_path_routes ORDER BY timestamp DESC"
    params: list = []
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    return [
        {
            "transaction_hash": row[0],
            "accounts": json.loads(row[1]),
            "hop_count": row[2],
            "cycle_volume": row[3],
            "is_atomic_self_payment": bool(row[4]),
            "touches_pool": bool(row[5]),
            "timestamp": row[6],
        }
        for row in rows
    ]


def save_rings(rings: list[dict], db_path: str | None = None) -> None:
    """Persist `find_wash_rings` output from the latest pipeline run."""
    if not rings:
        return
    init_db(db_path)
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO wash_rings
                (accounts_json, total_volume, cycle_volume, avg_trade_count,
                 timing_tightness, truncated, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    json.dumps(r["accounts"]),
                    r["total_volume"],
                    r["cycle_volume"],
                    r.get("avg_trade_count", 0.0),
                    r.get("timing_tightness", 0.0),
                    int(r.get("truncated", False)),
                    ts,
                )
                for r in rings
            ],
        )
        conn.commit()


def get_rings(
    limit: int | None = None,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    """Return detected wash-trading rings, most recent first, paginated."""
    init_db(db_path)
    query = (
        "SELECT accounts_json, total_volume, cycle_volume, avg_trade_count, "
        "timing_tightness, truncated, detected_at FROM wash_rings ORDER BY detected_at DESC"
    )
    params: list = []
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    return [
        {
            "accounts": json.loads(row[0]),
            "total_volume": row[1],
            "cycle_volume": row[2],
            "avg_trade_count": row[3],
            "timing_tightness": row[4],
            "truncated": bool(row[5]),
            "detected_at": row[6],
        }
        for row in rows
    ]


if __name__ == "__main__":
    init_db()
    print(f"Initialized risk score database at {settings.db_path}")
