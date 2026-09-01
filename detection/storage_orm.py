"""SQLAlchemy Core refactoring for Postgres/SQLite compatibility.

This is the beginning of Phase 1 of the migration plan. We use SQLAlchemy Core
to abstract the database engine and SQL dialects, replacing raw sqlite3 usage.
"""

from typing import List
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    String,
    Float,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import settings
from ingestion.data_models import Trade

metadata = MetaData()

trades_table = Table(
    'trades', metadata,
    Column('paging_token', String, primary_key=True),
    Column('trade_id', String, nullable=False),
    Column('ledger_close_time', String, nullable=False),
    Column('base_account', String, nullable=False),
    Column('counter_account', String),
    Column('base_asset_code', String, nullable=False),
    Column('base_asset_issuer', String),
    Column('counter_asset_code', String, nullable=False),
    Column('counter_asset_issuer', String),
    Column('base_amount', Float, nullable=False),
    Column('counter_amount', Float, nullable=False),
    Column('price', Float, nullable=False),
    Column('base_is_seller', Integer, nullable=False),
    Column('trade_type', String, nullable=False),
    Column('liquidity_pool_id', String),
    Column('transaction_hash', String),
    Column('path_payment_id', String),
    Column('hop_index', Integer)
)


class RiskScoreStoreOrm:
    """SQLAlchemy-backed store used by scoring and historical trade ingestion.
    
    Supports both PostgreSQL and SQLite by using dialect-specific UPSERT
    constructs (`ON CONFLICT DO NOTHING`).
    """

    def __init__(self, db_url: str | None = None) -> None:
        self.db_url = db_url or settings.db_url
        self.engine = create_engine(self.db_url, pool_pre_ping=True)
        
        # Apply SQLite specific pragmas if using SQLite
        if self.engine.dialect.name == "sqlite":
            from sqlalchemy import event
            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()
                
        # Ensure tables exist (Alembic should handle this in production)
        metadata.create_all(self.engine)

    def upsert_trades(self, trades: List[Trade]) -> int:
        """Insert validated trades, ignoring existing paging tokens."""
        if not trades:
            return 0
            
        rows = [
            {
                "paging_token": trade.paging_token or trade.id,
                "trade_id": trade.id,
                "ledger_close_time": trade.ledger_close_time.isoformat(),
                "base_account": trade.base_account,
                "counter_account": trade.counter_account,
                "base_asset_code": trade.base_asset.code,
                "base_asset_issuer": trade.base_asset.issuer,
                "counter_asset_code": trade.counter_asset.code,
                "counter_asset_issuer": trade.counter_asset.issuer,
                "base_amount": trade.base_amount,
                "counter_amount": trade.counter_amount,
                "price": trade.price,
                "base_is_seller": int(trade.base_is_seller),
                "trade_type": trade.trade_type.value,
                "liquidity_pool_id": trade.liquidity_pool_id,
                "transaction_hash": trade.transaction_hash,
                "path_payment_id": trade.path_payment_id,
                "hop_index": trade.hop_index,
            }
            for trade in trades
        ]
        
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                stmt = pg_insert(trades_table).values(rows).on_conflict_do_nothing(
                    index_elements=['paging_token']
                )
            else:
                stmt = sqlite_insert(trades_table).values(rows).on_conflict_do_nothing(
                    index_elements=['paging_token']
                )
                
            result = conn.execute(stmt)
            return result.rowcount

