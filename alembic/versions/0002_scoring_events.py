"""Add scoring_events append-only audit table (Issue #297).

Revision ID: 0002_scoring_events
Revises: 0001_initial_schema
Create Date: 2026-06-30

Adds the event-sourced scoring audit log table with:
- BEFORE UPDATE / BEFORE DELETE triggers enforcing append-only semantics.
- Indexes on (wallet, occurred_at) and occurred_at for efficient queries.
"""
from __future__ import annotations

from alembic import op

# revision identifiers
revision = "0002_scoring_events"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS scoring_events (
            event_id         TEXT PRIMARY KEY,
            wallet           TEXT NOT NULL,
            namespace_id     TEXT NOT NULL,
            score            INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
            previous_score   INTEGER,
            feature_snapshot TEXT NOT NULL,
            model_version    TEXT NOT NULL,
            triggered_by     TEXT NOT NULL,
            actor_id         TEXT,
            chain_hash       TEXT NOT NULL UNIQUE,
            occurred_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_se_wallet
        ON scoring_events (wallet, occurred_at)
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_se_occurred_at
        ON scoring_events (occurred_at)
        """
    )

    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS prevent_scoring_event_update
        BEFORE UPDATE ON scoring_events
        BEGIN
            SELECT RAISE(ABORT, 'scoring_events is append-only: UPDATE is not permitted');
        END
        """
    )

    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS prevent_scoring_event_delete
        BEFORE DELETE ON scoring_events
        BEGIN
            SELECT RAISE(ABORT, 'scoring_events is append-only: DELETE is not permitted');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prevent_scoring_event_delete")
    op.execute("DROP TRIGGER IF EXISTS prevent_scoring_event_update")
    op.execute("DROP INDEX IF EXISTS idx_se_occurred_at")
    op.execute("DROP INDEX IF EXISTS idx_se_wallet")
    op.execute("DROP TABLE IF EXISTS scoring_events")
