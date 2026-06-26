"""Initial schema — convert all existing CREATE TABLE statements.

Revision ID: 0001
Revises: (none)
Create Date: 2026-06-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_scores",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("benford_flag", sa.Integer, nullable=False),
        sa.Column("ml_flag", sa.Integer, nullable=False),
        sa.Column("confidence", sa.Integer, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("shap_json", sa.Text),
    )
    op.create_index("idx_risk_scores_wallet", "risk_scores", ["wallet"])
    op.create_index("idx_risk_scores_asset_pair", "risk_scores", ["asset_pair"])

    op.create_table(
        "on_chain_submissions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("tx_hash", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_message", sa.Text),
        sa.Column("submitted_at", sa.Text, nullable=False),
    )
    op.create_index("idx_submissions_wallet", "on_chain_submissions", ["wallet"])
    op.create_index("idx_submissions_status", "on_chain_submissions", ["status"])

    op.create_table(
        "pair_correlations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("pair_a", sa.Text, nullable=False),
        sa.Column("pair_b", sa.Text, nullable=False),
        sa.Column("correlation_r", sa.Float, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("shared_wallet_count", sa.Integer, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
    )
    op.create_index("idx_pair_correlations_pair_a", "pair_correlations", ["pair_a"])
    op.create_index("idx_pair_correlations_pair_b", "pair_correlations", ["pair_b"])

    op.create_table(
        "feature_vectors",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("features_json", sa.Text, nullable=False),
        sa.Column("shap_json", sa.Text),
        sa.Column("timestamp", sa.Text, nullable=False),
    )
    op.create_index("idx_feature_vectors_wallet", "feature_vectors", ["wallet"])
    op.create_index("idx_feature_vectors_asset_pair", "feature_vectors", ["asset_pair"])

    op.create_table(
        "liquidity_pool_trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.Text, nullable=False),
        sa.Column("pool_id", sa.Text, nullable=False),
        sa.Column("base_account", sa.Text, nullable=False),
        sa.Column("base_asset_pair", sa.Text, nullable=False),
        sa.Column("counter_asset_pair", sa.Text, nullable=False),
        sa.Column("base_amount", sa.Float, nullable=False),
        sa.Column("counter_amount", sa.Float, nullable=False),
        sa.Column("base_is_seller", sa.Integer, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
    )
    op.create_index("idx_lp_trades_pool_id", "liquidity_pool_trades", ["pool_id"])
    op.create_index("idx_lp_trades_base_account", "liquidity_pool_trades", ["base_account"])

    op.create_table(
        "path_payments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("payment_id", sa.Text, nullable=False),
        sa.Column("transaction_hash", sa.Text, nullable=False),
        sa.Column("source_account", sa.Text, nullable=False),
        sa.Column("destination_account", sa.Text, nullable=False),
        sa.Column("source_asset_pair", sa.Text, nullable=False),
        sa.Column("destination_asset_pair", sa.Text, nullable=False),
        sa.Column("source_amount", sa.Float, nullable=False),
        sa.Column("destination_amount", sa.Float, nullable=False),
        sa.Column("hop_count", sa.Integer, nullable=False),
        sa.Column("strict_send", sa.Integer, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
    )
    op.create_index("idx_path_payments_source", "path_payments", ["source_account"])
    op.create_index("idx_path_payments_tx_hash", "path_payments", ["transaction_hash"])

    op.create_table(
        "circular_path_routes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("transaction_hash", sa.Text, nullable=False),
        sa.Column("accounts_json", sa.Text, nullable=False),
        sa.Column("hop_count", sa.Integer, nullable=False),
        sa.Column("cycle_volume", sa.Float, nullable=False),
        sa.Column("is_atomic_self_payment", sa.Integer, nullable=False),
        sa.Column("touches_pool", sa.Integer, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
    )
    op.create_index("idx_circular_routes_tx_hash", "circular_path_routes", ["transaction_hash"])

    op.create_table(
        "drift_reports",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("triggered_at", sa.Text, nullable=False),
        sa.Column("drift_detected", sa.Integer, nullable=False),
        sa.Column("psi_report_json", sa.Text, nullable=False),
        sa.Column("psi_threshold", sa.Float, nullable=False),
        sa.Column("min_drifted_features", sa.Integer, nullable=False),
    )
    op.create_index("idx_drift_reports_triggered_at", "drift_reports", ["triggered_at"])

    op.create_table(
        "retrain_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("triggered_at", sa.Text, nullable=False),
        sa.Column("drift_report_id", sa.Integer, sa.ForeignKey("drift_reports.id")),
        sa.Column("model_name", sa.Text, nullable=False),
        sa.Column("old_version", sa.Text),
        sa.Column("new_version", sa.Text),
        sa.Column("old_auc_roc", sa.Float),
        sa.Column("new_auc_roc", sa.Float),
        sa.Column("promoted", sa.Integer),
    )

    op.create_table(
        "robustness_reports",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("epsilon", sa.Float, nullable=False),
        sa.Column("steps", sa.Integer, nullable=False),
        sa.Column("n_samples", sa.Integer, nullable=False),
        sa.Column("report_json", sa.Text, nullable=False),
    )

    op.create_table(
        "committee_members",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("member_id", sa.Text, nullable=False, unique=True),
        sa.Column("added_at", sa.Text, nullable=False),
    )

    op.create_table(
        "score_disputes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("disputed_score", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("resolved_at", sa.Text),
    )
    op.create_index("idx_disputes_wallet", "score_disputes", ["wallet"])
    op.create_index("idx_disputes_status", "score_disputes", ["status"])

    op.create_table(
        "score_overrides",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("dispute_id", sa.Integer, sa.ForeignKey("score_disputes.id")),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("override_score", sa.Integer, nullable=False),
        sa.Column("applied_by", sa.Text, nullable=False),
        sa.Column("applied_at", sa.Text, nullable=False),
    )

    op.create_table(
        "runtime_config",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )

    op.create_table(
        "governance_proposals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("proposal_type", sa.Text, nullable=False),
        sa.Column("proposed_by", sa.Text, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("deadline_at", sa.Text, nullable=False),
        sa.Column("resolved_at", sa.Text),
        sa.Column("votes_for", sa.Integer),
        sa.Column("votes_against", sa.Integer),
    )
    op.create_index("idx_governance_proposals_status", "governance_proposals", ["status"])

    op.create_table(
        "wallet_feature_states",
        sa.Column("wallet", sa.Text, primary_key=True),
        sa.Column("feature_json", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )

    op.create_table(
        "wash_rings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ring_id", sa.Text, nullable=False),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("detected_at", sa.Text, nullable=False),
        sa.Column("ring_score", sa.Integer, nullable=False),
    )
    op.create_index("idx_wash_rings_ring_id", "wash_rings", ["ring_id"])
    op.create_index("idx_wash_rings_wallet", "wash_rings", ["wallet"])

    op.create_table(
        "bridge_transfers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("transfer_id", sa.Text, nullable=False),
        sa.Column("source_chain", sa.Text, nullable=False),
        sa.Column("dest_chain", sa.Text, nullable=False),
        sa.Column("source_address", sa.Text, nullable=False),
        sa.Column("dest_address", sa.Text, nullable=False),
        sa.Column("asset", sa.Text, nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("risk_indicators_json", sa.Text),
    )
    op.create_index("idx_bridge_transfers_transfer_id", "bridge_transfers", ["transfer_id"])
    op.create_index("idx_bridge_transfers_source_address", "bridge_transfers", ["source_address"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("alert_type", sa.Text, nullable=False),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("pool_id", sa.Text),
        sa.Column("detail_json", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
    )
    op.create_index("idx_alerts_wallet", "alerts", ["wallet"])
    op.create_index("idx_alerts_alert_type", "alerts", ["alert_type"])
    op.create_index("idx_alerts_timestamp", "alerts", ["timestamp"])

    op.create_table(
        "path_payment_cycles",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("transaction_hash", sa.Text, nullable=False),
        sa.Column("source_account", sa.Text, nullable=False),
        sa.Column("cycle_accounts_json", sa.Text, nullable=False),
        sa.Column("hop_count", sa.Integer, nullable=False),
        sa.Column("detected_at", sa.Text, nullable=False),
    )
    op.create_index("idx_ppc_tx_hash", "path_payment_cycles", ["transaction_hash"])
    op.create_index("idx_ppc_source_account", "path_payment_cycles", ["source_account"])

    # Auxiliary tables from other modules
    op.create_table(
        "rolling_window_checkpoints",
        sa.Column("wallet", sa.Text, primary_key=True),
        sa.Column("trades_json", sa.Text, nullable=False),
        sa.Column("last_score", sa.Integer),
        sa.Column("updated_at", sa.TIMESTAMP, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.create_table(
        "bandit_state",
        sa.Column("arm", sa.Text, primary_key=True),
        sa.Column("alpha", sa.Float, nullable=False),
        sa.Column("beta_val", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )

    op.create_table(
        "scoring_feedback",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("asset_pair", sa.Text, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("label", sa.Integer, nullable=False),
        sa.Column("model_name", sa.Text, nullable=False),
        sa.Column("feedback_at", sa.Text, nullable=False),
    )
    op.create_index("idx_scoring_feedback_wallet", "scoring_feedback", ["wallet"])
    op.create_index("idx_scoring_feedback_model", "scoring_feedback", ["model_name"])

    op.create_table(
        "webhook_subscribers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("event_types", sa.Text, nullable=False),
        sa.Column("secret", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("active", sa.Integer, nullable=False),
    )

    op.create_table(
        "webhook_delivery_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("subscriber_id", sa.Integer, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("next_attempt_at", sa.Text, nullable=False),
    )
    op.create_index("idx_wdq_status", "webhook_delivery_queue", ["status"])
    op.create_index("idx_wdq_next_attempt", "webhook_delivery_queue", ["next_attempt_at"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key_hash", sa.Text, nullable=False, unique=True),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text),
        sa.Column("revoked", sa.Integer, nullable=False),
    )
    op.create_index("idx_api_keys_key_hash", "api_keys", ["key_hash"])
    op.create_index("idx_api_keys_namespace", "api_keys", ["namespace"])

    op.create_table(
        "batch_jobs",
        sa.Column("job_id", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column("done", sa.Integer, nullable=False),
        sa.Column("results_json", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("completed_at", sa.Text),
    )

    op.create_table(
        "retrain_jobs",
        sa.Column("job_id", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("completed_at", sa.Text),
    )

    op.create_table(
        "alert_suppressions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text),
    )
    op.create_index("idx_suppressions_wallet", "alert_suppressions", ["wallet"])
    op.create_index("idx_suppressions_expires_at", "alert_suppressions", ["expires_at"])

    op.create_table(
        "federated_audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("participant_id", sa.Text, nullable=False),
        sa.Column("round_id", sa.Text),
        sa.Column("payload_hash", sa.Text),
        sa.Column("timestamp", sa.Text, nullable=False),
    )

    op.create_table(
        "federated_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value_json", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )

    op.create_table(
        "evasion_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("strategy", sa.Text, nullable=False),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("score_before", sa.Integer, nullable=False),
        sa.Column("score_after", sa.Integer, nullable=False),
        sa.Column("detected_at", sa.Text, nullable=False),
    )

    op.create_table(
        "feature_distribution_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("feature_name", sa.Text, nullable=False),
        sa.Column("snapshot_json", sa.Text, nullable=False),
        sa.Column("recorded_at", sa.Text, nullable=False),
    )

    op.create_table(
        "feedback_labels",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.Text, nullable=False),
        sa.Column("label", sa.Integer, nullable=False),
        sa.Column("score", sa.Integer),
        sa.Column("labelled_at", sa.Text, nullable=False),
    )

    op.create_table(
        "degradation_alerts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("metric", sa.Text, nullable=False),
        sa.Column("baseline", sa.Float, nullable=False),
        sa.Column("current_value", sa.Float, nullable=False),
        sa.Column("alerted_at", sa.Text, nullable=False),
    )

    op.create_table(
        "causal_ate_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("feature", sa.Text, nullable=False),
        sa.Column("ate", sa.Float, nullable=False),
        sa.Column("cached_at", sa.Text, nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("wallet", sa.Text),
        sa.Column("score", sa.Integer),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("entry_hash", sa.Text, nullable=False, unique=True),
    )
    op.create_index("idx_audit_log_event_type", "audit_log", ["event_type"])
    op.create_index("idx_audit_log_timestamp", "audit_log", ["timestamp"])


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("audit_log")
    op.drop_table("causal_ate_cache")
    op.drop_table("degradation_alerts")
    op.drop_table("feedback_labels")
    op.drop_table("feature_distribution_snapshots")
    op.drop_table("evasion_events")
    op.drop_table("federated_state")
    op.drop_table("federated_audit_log")
    op.drop_table("alert_suppressions")
    op.drop_table("retrain_jobs")
    op.drop_table("batch_jobs")
    op.drop_table("api_keys")
    op.drop_table("webhook_delivery_queue")
    op.drop_table("webhook_subscribers")
    op.drop_table("scoring_feedback")
    op.drop_table("bandit_state")
    op.drop_table("rolling_window_checkpoints")
    op.drop_table("path_payment_cycles")
    op.drop_table("alerts")
    op.drop_table("bridge_transfers")
    op.drop_table("wash_rings")
    op.drop_table("wallet_feature_states")
    op.drop_table("governance_proposals")
    op.drop_table("runtime_config")
    op.drop_table("score_overrides")
    op.drop_table("score_disputes")
    op.drop_table("committee_members")
    op.drop_table("robustness_reports")
    op.drop_table("retrain_runs")
    op.drop_table("drift_reports")
    op.drop_table("circular_path_routes")
    op.drop_table("path_payments")
    op.drop_table("liquidity_pool_trades")
    op.drop_table("feature_vectors")
    op.drop_table("pair_correlations")
    op.drop_table("on_chain_submissions")
    op.drop_table("risk_scores")
