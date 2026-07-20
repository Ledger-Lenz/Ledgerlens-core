"""Add case_assignments table and analyst_feedback verdicts table (Issue #200).

Revision ID: 0003_case_assignments
Revises: 0002_scoring_events
Create Date: 2026-07-17

Adds the case_assignments table for tracking wallet review assignments,
soft locks with expiry, and SLA metrics.  Also adds the analyst_feedback
verdicts table (distinct from feedback_store's analyst_feedback) with
verdict, notes, and analyst_key_hash columns.
"""
from __future__ import annotations

from alembic import op

# revision identifiers
revision = "0003_case_assignments"
down_revision = "0002_scoring_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS case_assignments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet          TEXT NOT NULL,
            asset_pair      TEXT NOT NULL,
            analyst_key_hash TEXT NOT NULL,
            assigned_at     TEXT NOT NULL,
            lock_expires_at TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'assigned'
                                CHECK(status IN ('assigned', 'released', 'resolved')),
            released_at     TEXT,
            resolved_at     TEXT
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_case_assignments_wallet
        ON case_assignments (wallet, asset_pair)
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_case_assignments_analyst
        ON case_assignments (analyst_key_hash, status)
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_case_assignments_status
        ON case_assignments (status)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS analyst_feedback (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet          TEXT NOT NULL,
            asset_pair      TEXT NOT NULL,
            verdict         TEXT NOT NULL,
            notes           TEXT,
            analyst_key_hash TEXT NOT NULL,
            submitted_at    TEXT NOT NULL,
            review_started_at TEXT
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_analyst_feedback_wallet
        ON analyst_feedback (wallet)
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_analyst_feedback_submitted
        ON analyst_feedback (submitted_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_analyst_feedback_submitted")
    op.execute("DROP INDEX IF EXISTS idx_analyst_feedback_wallet")
    op.execute("DROP TABLE IF EXISTS analyst_feedback")
    op.execute("DROP INDEX IF EXISTS idx_case_assignments_status")
    op.execute("DROP INDEX IF EXISTS idx_case_assignments_analyst")
    op.execute("DROP INDEX IF EXISTS idx_case_assignments_wallet")
    op.execute("DROP TABLE IF EXISTS case_assignments")
