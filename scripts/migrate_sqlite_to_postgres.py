"""Migration script to export data from local SQLite to Postgres.

Usage:
    python scripts/migrate_sqlite_to_postgres.py --sqlite-db data/ledgerlens.db --pg-url postgresql://user:pass@host:5432/ledgerlens
"""

import argparse
import logging
import sqlite3
import pandas as pd
from sqlalchemy import create_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TABLES = [
    "risk_scores",
    "on_chain_submissions",
    "pair_correlations",
    "trades",
    "feature_vectors",
    "liquidity_pool_trades",
    "path_payments",
    "circular_path_routes",
    "drift_reports",
    "retrain_runs",
    "robustness_reports",
    "committee_members",
    "score_disputes",
    "score_overrides",
    "runtime_config",
    "governance_proposals",
    "governance_votes",
    "governance_committee",
    "wallet_feature_states",
    "wash_rings",
    "bridge_transfers",
    "alerts",
    "path_payment_cycles",
    "soroban_dead_letters",
    "benford_baselines",
    "case_assignments",
    "analyst_feedback",
    "compliance_exports"
]

def migrate(sqlite_path: str, pg_url: str, chunksize: int = 10000):
    logger.info(f"Connecting to SQLite at {sqlite_path}")
    sqlite_conn = sqlite3.connect(sqlite_path)
    
    logger.info(f"Connecting to Postgres at {pg_url}")
    pg_engine = create_engine(pg_url)
    
    with pg_engine.begin() as pg_conn:
        for table in TABLES:
            logger.info(f"Migrating table: {table}")
            try:
                # Read total count for logging
                cursor = sqlite_conn.cursor()
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                total_rows = cursor.fetchone()[0]
                logger.info(f"Found {total_rows} rows in {table}")
                
                if total_rows == 0:
                    continue
                
                # Stream via pandas
                chunks = pd.read_sql(f"SELECT * FROM {table}", sqlite_conn, chunksize=chunksize)
                
                processed = 0
                for chunk in chunks:
                    chunk.to_sql(table, pg_conn, if_exists="append", index=False)
                    processed += len(chunk)
                    logger.info(f"  ... inserted {processed}/{total_rows} rows")
                    
            except sqlite3.OperationalError as e:
                logger.warning(f"Skipping table {table} due to error (might not exist): {e}")

    sqlite_conn.close()
    logger.info("Migration complete!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-db", required=True)
    parser.add_argument("--pg-url", required=True)
    parser.add_argument("--chunksize", type=int, default=10000)
    args = parser.parse_args()
    
    migrate(args.sqlite_db, args.pg_url, args.chunksize)
