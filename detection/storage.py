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
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum

import pandas as pd

from config.settings import settings
from detection.risk_score import RiskScore
from ingestion.data_models import BridgeTransfer, PathPayment, Trade

logger = logging.getLogger("ledgerlens.storage")


class AlertType(str, Enum):
    """Taxonomy of manipulation alerts surfaced via the `/alerts` API.

    Add new alert categories here; the value is the string stored in the
    `alerts.alert_type` column and accepted by the `/alerts?alert_type=` query.
    """

    WASH_TRADING = "WASH_TRADING"
    CIRCULAR_ROUTE = "CIRCULAR_ROUTE"
    POOL_MANIPULATION = "POOL_MANIPULATION"
    SANDWICH_ATTACK = "SANDWICH_ATTACK"
    PATH_PAYMENT_CYCLE = "PATH_PAYMENT_CYCLE"


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
    (
        8,
        "add wallet_feature_states table for streaming feature store",
        """
        CREATE TABLE IF NOT EXISTS wallet_feature_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            asset_pair TEXT NOT NULL,
            state_json TEXT NOT NULL,
            last_updated TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_feature_states_key ON wallet_feature_states (wallet, asset_pair);
        CREATE INDEX IF NOT EXISTS idx_wallet_feature_states_updated ON wallet_feature_states (last_updated);
        """,
    ),
    (
        9,
        "add wash_rings table for wash trading ring detection",
        """
        CREATE TABLE IF NOT EXISTS wash_rings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accounts_json TEXT NOT NULL,
            total_volume REAL NOT NULL,
            cycle_volume REAL NOT NULL,
            avg_trade_count REAL NOT NULL,
            timing_tightness REAL NOT NULL,
            truncated INTEGER NOT NULL,
            detected_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wash_rings_detected_at ON wash_rings (detected_at);
        """,
    ),
    (
        10,
        "add bridge_transfers table for cross-chain wallet linking",
        """
        CREATE TABLE IF NOT EXISTS bridge_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL,
            direction TEXT NOT NULL,
            evm_wallet TEXT NOT NULL,
            stellar_wallet TEXT NOT NULL,
            amount_usd REAL,
            token TEXT NOT NULL,
            tx_hash_evm TEXT NOT NULL,
            tx_hash_stellar TEXT,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bridge_transfers_stellar_wallet
            ON bridge_transfers (stellar_wallet);
        CREATE INDEX IF NOT EXISTS idx_bridge_transfers_evm_wallet
            ON bridge_transfers (evm_wallet);
        CREATE INDEX IF NOT EXISTS idx_bridge_transfers_timestamp
            ON bridge_transfers (timestamp);
        """,
    ),
    (
        11,
        "add alerts table for typed manipulation alerts",
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            wallet TEXT NOT NULL,
            asset_pair TEXT NOT NULL,
            pool_id TEXT,
            detail_json TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_wallet ON alerts (wallet);
        CREATE INDEX IF NOT EXISTS idx_alerts_alert_type ON alerts (alert_type);
        CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts (timestamp);
        """,
    ),
    (
        12,
        "add path_payment_cycles table for multi-hop cycle detection",
        """
        CREATE TABLE IF NOT EXISTS path_payment_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accounts_json TEXT NOT NULL,
            cycle_path_json TEXT NOT NULL,
            cycle_value_xlm REAL NOT NULL,
            cycle_length INTEGER NOT NULL,
            completed_in_seconds REAL NOT NULL,
            asset_diversity INTEGER NOT NULL,
            cycle_metadata_json TEXT NOT NULL,
            detected_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_path_payment_cycles_detected_at
            ON path_payment_cycles (detected_at);
        """,
    ),
    (
        13,
        "add integrity verification columns to bridge_transfers",
        """
        ALTER TABLE bridge_transfers ADD COLUMN canonical_hash TEXT;
        ALTER TABLE bridge_transfers ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'disabled';
        ALTER TABLE bridge_transfers ADD COLUMN verified_at TIMESTAMP;
        """,
    ),
    (
        14,
        "add soroban_dead_letters table for DLQ",
        """
        CREATE TABLE IF NOT EXISTS soroban_dead_letters (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet          TEXT NOT NULL,
            asset_pair      TEXT NOT NULL,
            score           INTEGER NOT NULL,
            ledger_timestamp INTEGER NOT NULL,
            error_message   TEXT,
            status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'replayed', 'failed')),
            created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            replayed_at     TIMESTAMP,
            replay_tx_hash  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dlq_status ON soroban_dead_letters(status);
        CREATE INDEX IF NOT EXISTS idx_dlq_created ON soroban_dead_letters(created_at);
        """,
    ),
    (
        15,
        "add account_metadata_cache table for enriched wallet metadata",
        """
        CREATE TABLE IF NOT EXISTS account_metadata_cache (
            account_id TEXT PRIMARY KEY,
            funding_source TEXT,
            created_at TEXT,
            home_domain TEXT,
            num_signers INTEGER NOT NULL DEFAULT 1,
            low_threshold INTEGER NOT NULL DEFAULT 0,
            med_threshold INTEGER NOT NULL DEFAULT 0,
            high_threshold INTEGER NOT NULL DEFAULT 0,
            signer_keys_json TEXT NOT NULL DEFAULT '[]',
            fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_account_metadata_fetched ON account_metadata_cache (fetched_at);
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


class RiskScoreStore:
    """SQLite store used by scoring and historical trade ingestion.

    Historical trades are inserted in page-sized batches with
    ``INSERT OR IGNORE`` so replayed pages and adjacent boundary duplicates
    are harmless.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.db_path
        init_db(self.db_path)
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")

    def upsert_trades(self, trades: list[Trade]) -> int:
        """Insert validated trades, ignoring existing paging tokens."""
        if not trades:
            return 0
        rows = [
            (
                trade.paging_token or trade.id,
                trade.id,
                trade.ledger_close_time.isoformat(),
                trade.base_account,
                trade.counter_account,
                trade.base_asset.code,
                trade.base_asset.issuer,
                trade.counter_asset.code,
                trade.counter_asset.issuer,
                trade.base_amount,
                trade.counter_amount,
                trade.price,
                int(trade.base_is_seller),
                trade.trade_type.value,
                trade.liquidity_pool_id,
                trade.transaction_hash,
                trade.path_payment_id,
                trade.hop_index,
            )
            for trade in trades
        ]
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO trades (
                    paging_token, trade_id, ledger_close_time, base_account,
                    counter_account, base_asset_code, base_asset_issuer,
                    counter_asset_code, counter_asset_issuer, base_amount,
                    counter_amount, price, base_is_seller, trade_type,
                    liquidity_pool_id, transaction_hash, path_payment_id, hop_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return conn.total_changes - before


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


def save_path_payment_cycles(cycles: list[dict], db_path: str | None = None) -> None:
    """Persist `detect_path_payment_cycles` output from the latest pipeline run.

    The full cycle dict is stored in `cycle_metadata_json` so consumers can
    recover the originating account, transaction hashes and asset hops; the
    scalar columns mirror the alert detail for cheap filtering/aggregation.
    """
    if not cycles:
        return
    init_db(db_path)
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO path_payment_cycles
                (accounts_json, cycle_path_json, cycle_value_xlm, cycle_length,
                 completed_in_seconds, asset_diversity, cycle_metadata_json, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    json.dumps(c.get("accounts", [])),
                    json.dumps(c.get("cycle_path", [])),
                    float(c.get("cycle_value_xlm", 0.0)),
                    int(c.get("cycle_length", 0)),
                    float(c.get("completed_in_seconds", 0.0)),
                    int(c.get("asset_diversity", 0)),
                    json.dumps(c),
                    ts,
                )
                for c in cycles
            ],
        )
        conn.commit()


def get_path_payment_cycles(
    limit: int | None = None,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    """Return detected multi-hop path-payment cycles, most recent first."""
    init_db(db_path)
    query = (
        "SELECT accounts_json, cycle_path_json, cycle_value_xlm, cycle_length, "
        "completed_in_seconds, asset_diversity, cycle_metadata_json, detected_at "
        "FROM path_payment_cycles ORDER BY detected_at DESC, id DESC"
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
            "cycle_path": json.loads(row[1]),
            "cycle_value_xlm": row[2],
            "cycle_length": row[3],
            "completed_in_seconds": row[4],
            "asset_diversity": row[5],
            "metadata": json.loads(row[6]),
            "detected_at": row[7],
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
                    json.dumps(r.get("accounts", [])),
                    float(r.get("total_volume", 0.0)),
                    float(r.get("cycle_volume", 0.0)),
                    float(r.get("avg_trade_count", 0.0)),
                    float(r.get("timing_tightness", 0.0)),
                    int(bool(r.get("truncated", False))),
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


def save_feature_state(state, db_path: str | None = None) -> None:
    """Persist a WalletFeatureState to cold storage (SQLite).

    Args:
        state: WalletFeatureState instance to persist.
        db_path: Optional database path; uses settings.db_path if not provided.
    """
    init_db(db_path)
    state_json = state.model_dump_json()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO wallet_feature_states
                (wallet, asset_pair, state_json, last_updated)
            VALUES (?, ?, ?, ?)
            """,
            (state.wallet, state.asset_pair, state_json, state.last_updated.isoformat()),
        )
        conn.commit()


def get_feature_state(wallet: str, asset_pair: str, db_path: str | None = None):
    """Retrieve a WalletFeatureState from cold storage.

    Returns None if not found.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT state_json FROM wallet_feature_states WHERE wallet = ? AND asset_pair = ?",
            (wallet, asset_pair),
        ).fetchone()

    if row is None:
        return None

    from detection.feature_store import WalletFeatureState
    return WalletFeatureState.model_validate_json(row[0])


def promote_cold_to_hot(feature_store, batch_size: int = 100, db_path: str | None = None) -> int:
    """Load most recently updated feature states from cold storage and write to Redis.

    Returns the count of states promoted.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT state_json FROM wallet_feature_states
            ORDER BY last_updated DESC
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()

    count = 0
    from detection.feature_store import WalletFeatureState
    for row in rows:
        try:
            state = WalletFeatureState.model_validate_json(row[0])
            feature_store.set_state(state)
            count += 1
        except Exception as e:
            logger.error(f"Error promoting feature state: {e}")

    return count


def save_bridge_transfer(transfer: BridgeTransfer, db_path: str | None = None) -> None:
    """Persist a single bridge transfer record."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bridge_transfers
                (chain, direction, evm_wallet, stellar_wallet, amount_usd, token,
                 tx_hash_evm, tx_hash_stellar, timestamp,
                 canonical_hash, verification_status, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transfer.chain,
                transfer.direction,
                transfer.evm_wallet,
                transfer.stellar_wallet,
                transfer.amount_usd,
                transfer.token,
                transfer.tx_hash_evm,
                transfer.tx_hash_stellar,
                transfer.timestamp.isoformat(),
                transfer.canonical_hash,
                transfer.verification_status,
                transfer.verified_at.isoformat() if transfer.verified_at else None,
            ),
        )
        conn.commit()


def save_bridge_transfers(transfers: list[BridgeTransfer], db_path: str | None = None) -> None:
    """Persist a batch of bridge transfer records."""
    if not transfers:
        return
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO bridge_transfers
                (chain, direction, evm_wallet, stellar_wallet, amount_usd, token,
                 tx_hash_evm, tx_hash_stellar, timestamp,
                 canonical_hash, verification_status, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    t.chain,
                    t.direction,
                    t.evm_wallet,
                    t.stellar_wallet,
                    t.amount_usd,
                    t.token,
                    t.tx_hash_evm,
                    t.tx_hash_stellar,
                    t.timestamp.isoformat(),
                    t.canonical_hash,
                    t.verification_status,
                    t.verified_at.isoformat() if t.verified_at else None,
                )
                for t in transfers
            ],
        )
        conn.commit()


def get_bridge_transfers(
    stellar_wallet: str | None = None,
    evm_wallet: str | None = None,
    since_days: int = 90,
    db_path: str | None = None,
) -> list[BridgeTransfer]:
    """Return bridge transfers filtered by wallet and recency."""
    init_db(db_path)
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    conditions = ["timestamp >= ?"]
    params: list = [cutoff]
    if stellar_wallet is not None:
        conditions.append("stellar_wallet = ?")
        params.append(stellar_wallet)
    if evm_wallet is not None:
        conditions.append("evm_wallet = ?")
        params.append(evm_wallet)

    where = " AND ".join(conditions)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT chain, direction, evm_wallet, stellar_wallet, amount_usd, token,
                   tx_hash_evm, tx_hash_stellar, timestamp,
                   canonical_hash, verification_status, verified_at
            FROM bridge_transfers
            WHERE {where}
            ORDER BY timestamp DESC
            """,
            tuple(params),
        ).fetchall()

    return [
        BridgeTransfer(
            chain=row[0],
            direction=row[1],
            evm_wallet=row[2],
            stellar_wallet=row[3],
            amount_usd=row[4],
            token=row[5],
            tx_hash_evm=row[6],
            tx_hash_stellar=row[7],
            timestamp=datetime.fromisoformat(row[8]),
            canonical_hash=row[9],
            verification_status=row[10] if row[10] is not None else "disabled",
            verified_at=datetime.fromisoformat(row[11]) if row[11] else None,
        )
        for row in rows
    ]


def get_bridge_transfer_history(
    stellar_wallet: str,
    db_path: str | None = None,
) -> list[dict]:
    """Return the full bridge transfer history for a Stellar wallet as dicts."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT chain, direction, evm_wallet, stellar_wallet, amount_usd, token,
                   tx_hash_evm, tx_hash_stellar, timestamp
            FROM bridge_transfers
            WHERE stellar_wallet = ?
            ORDER BY timestamp DESC
            """,
            (stellar_wallet,),
        ).fetchall()

    return [
        {
            "chain": row[0],
            "direction": row[1],
            "evm_wallet": row[2],
            "stellar_wallet": row[3],
            "amount_usd_estimate": row[4],
            "token": row[5],
            "tx_hash_evm": row[6],
            "tx_hash_stellar": row[7],
            "timestamp": row[8],
        }
        for row in rows
    ]


def sandwich_candidates_to_alerts(candidates, asset_pair: str) -> list[dict]:
    """Convert `SandwichCandidate` objects into storable alert dicts.

    Each alert is attributed to the attacker account (`wallet`) and carries the
    sandwich-specific evidence (victim, profit, slippage, ledger ordering) in
    its ``detail`` payload.  See `detection.sandwich_engine`.
    """
    alerts: list[dict] = []
    for c in candidates:
        alerts.append(
            {
                "alert_type": AlertType.SANDWICH_ATTACK.value,
                "wallet": c.attacker,
                "asset_pair": asset_pair,
                "pool_id": c.pool_id,
                "detail": {
                    "victim": c.victim,
                    "profit_xlm": c.profit_xlm,
                    "slippage_inflicted": c.slippage_inflicted,
                    "ledger_sequence": c.ledger_sequence,
                    "buy_op_idx": c.buy_op_idx,
                    "victim_op_idx": c.victim_op_idx,
                    "sell_op_idx": c.sell_op_idx,
                },
            }
        )
    return alerts


def save_alerts(alerts: list[dict], db_path: str | None = None) -> None:
    """Persist typed manipulation alerts.

    Each alert dict must carry ``alert_type``, ``wallet``, ``asset_pair`` and a
    JSON-serialisable ``detail`` mapping; ``pool_id`` and ``timestamp`` (ISO 8601)
    are optional and default to ``None`` / now.

    Suppressed wallets are silently dropped before persistence (Issue #178).
    """
    if not alerts:
        return
    from detection.suppressions import filter_suppressed_alerts
    alerts = filter_suppressed_alerts(alerts, db_path=db_path)
    if not alerts:
        return
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO alerts
                (alert_type, wallet, asset_pair, pool_id, detail_json, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    a["alert_type"],
                    a["wallet"],
                    a["asset_pair"],
                    a.get("pool_id"),
                    json.dumps(a.get("detail", {})),
                    a.get("timestamp", now),
                )
                for a in alerts
            ],
        )
        conn.commit()


def get_alerts(
    alert_type: str | None = None,
    wallet: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    """Return stored typed alerts, most recent first, with optional filters.

    Filters by ``alert_type``, ``wallet`` and an inclusive ``[start, end]`` ISO
    8601 timestamp window when provided.  ``detail`` is returned parsed.
    """
    init_db(db_path)
    conditions: list[str] = []
    params: list = []
    if alert_type is not None:
        conditions.append("alert_type = ?")
        params.append(getattr(alert_type, "value", alert_type))
    if wallet is not None:
        conditions.append("wallet = ?")
        params.append(wallet)
    if start is not None:
        conditions.append("timestamp >= ?")
        params.append(start)
    if end is not None:
        conditions.append("timestamp <= ?")
        params.append(end)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
        SELECT alert_type, wallet, asset_pair, pool_id, detail_json, timestamp
        FROM alerts
        {where}
        ORDER BY timestamp DESC, id DESC
    """
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    with _connect(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    return [
        {
            "alert_type": row[0],
            "wallet": row[1],
            "asset_pair": row[2],
            "pool_id": row[3],
            "detail": json.loads(row[4]) if row[4] else {},
            "timestamp": row[5],
        }
        for row in rows
    ]


def get_score_history(
    wallet: str,
    start: str,
    end: str,
    db_path: str | None = None,
) -> list[dict]:
    """Return the risk-score time series for ``wallet`` within ``[start, end]``.

    ``start`` and ``end`` are inclusive ISO 8601 timestamps.  Rows are ordered
    oldest-first so the result reads as a chronological series, as required by
    the regulatory export layer (`detection.compliance_exporter`).
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT wallet, asset_pair, score, benford_flag, ml_flag, confidence, timestamp
            FROM risk_scores
            WHERE wallet = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
            """,
            (wallet, start, end),
        ).fetchall()

    return [
        {
            "wallet": row[0],
            "asset_pair": row[1],
            "score": row[2],
            "benford_flag": bool(row[3]),
            "ml_flag": bool(row[4]),
            "confidence": row[5],
            "timestamp": row[6],
        }
        for row in rows
    ]


def save_hop_payment_cycles(
    cycles: list,
    db_path: str | None = None,
) -> None:
    """Persist PathPaymentCycle records to the hop_payment_cycles table."""
    import dataclasses
    init_db(db_path)
    if not cycles:
        return
    with _connect(db_path) as conn:
        for cycle in cycles:
            hops = [
                {
                    "src_wallet": h.src_wallet,
                    "src_asset": h.src_asset,
                    "dst_wallet": h.dst_wallet,
                    "dst_asset": h.dst_asset,
                    "amount": h.amount,
                    "ledger_timestamp": h.ledger_timestamp.isoformat(),
                    "operation_id": h.operation_id,
                }
                for h in cycle.hops
            ]
            conn.execute(
                """
                INSERT INTO hop_payment_cycles
                    (origin_wallet, origin_asset, path_length, recovery_ratio,
                     cycle_duration_seconds, counterparty_overlap, cycle_score,
                     hop_json, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle.origin_wallet,
                    cycle.origin_asset,
                    cycle.path_length,
                    cycle.recovery_ratio,
                    cycle.cycle_duration_seconds,
                    cycle.counterparty_overlap,
                    cycle.cycle_score,
                    json.dumps(hops),
                    cycle.detected_at.isoformat(),
                ),
            )
        conn.commit()


def get_hop_payment_cycles(
    min_score: float = 0.6,
    wallet: str | None = None,
    limit: int = 100,
    db_path: str | None = None,
) -> list[dict]:
    """Retrieve PathPaymentCycle records from storage."""
    init_db(db_path)
    query = """
        SELECT id, origin_wallet, origin_asset, path_length, recovery_ratio,
               cycle_duration_seconds, counterparty_overlap, cycle_score,
               hop_json, detected_at
        FROM hop_payment_cycles
        WHERE cycle_score >= ?
    """
    params: list = [min_score]
    if wallet:
        query += " AND origin_wallet = ?"
        params.append(wallet)
    query += " ORDER BY cycle_score DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": r[0],
            "origin_wallet": r[1],
            "origin_asset": r[2],
            "path_length": r[3],
            "recovery_ratio": r[4],
            "cycle_duration_seconds": r[5],
            "counterparty_overlap": r[6],
            "cycle_score": r[7],
            "hops": json.loads(r[8]),
            "detected_at": r[9],
        }
        for r in rows
    ]


def log_krum_aggregation(
    round_number: int,
    n_clients: int,
    f_tolerance: int,
    m_selected: int,
    selected_indices: list,
    excluded_indices: list,
    krum_scores: list,
    db_path: str | None = None,
) -> None:
    """Persist one Krum aggregation round decision to fl_aggregation_log."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO fl_aggregation_log
              (round_number, n_clients, f_tolerance, m_selected,
               selected_indices, excluded_indices, krum_scores, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_number,
                n_clients,
                f_tolerance,
                m_selected,
                json.dumps(selected_indices),
                json.dumps(excluded_indices),
                json.dumps([float(s) for s in krum_scores]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def get_krum_aggregation_log(
    limit: int = 10,
    db_path: str | None = None,
) -> list[dict]:
    """Return the most recent Krum aggregation rounds, newest first."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT round_number, n_clients, f_tolerance, m_selected,
                   selected_indices, excluded_indices, krum_scores, recorded_at
            FROM fl_aggregation_log
            ORDER BY round_number DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "round_number": r[0],
            "n_clients": r[1],
            "f_tolerance": r[2],
            "m_selected": r[3],
            "selected_indices": json.loads(r[4]),
            "excluded_indices": json.loads(r[5]),
            "krum_scores": json.loads(r[6]),
            "recorded_at": r[7],
        }
        for r in rows
    ]


if __name__ == "__main__":
    init_db()
    print(f"Initialized risk score database at {settings.db_path}")
