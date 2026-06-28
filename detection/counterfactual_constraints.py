"""Feature mutability manifest for counterfactual explanation generation.

`detection.counterfactual_engine` must never propose a counterfactual that
asks a wallet operator to do something physically impossible (e.g. make an
account younger, or undo a cross-chain bridge transfer that already
happened) or to move a feature in a direction that would *increase*
suspicion. This module is the single source of truth for what each feature
is allowed to do during search.

Almost every mutable feature in this codebase is constructed so that
*higher values are more suspicious* (the model learns a positive
association with the wash-trading label), so almost every mutable
feature's constrained direction is "decrease". `direction="increase"` is
fully supported for the one feature where the opposite holds
(`cross_chain_time_lag_median_h`).
"""

from dataclasses import dataclass

from detection.feature_engineering import FEATURE_NAMES


@dataclass(frozen=True)
class FeatureConstraint:
    """Describes how a single feature may legally be perturbed.

    `direction` is only meaningful when `mutable` is True: "decrease" means
    the counterfactual may only lower the feature relative to its observed
    value, "increase" the reverse, and "any" permits movement in either
    direction (subject to `min_val`/`max_val`). `min_val`/`max_val` bound
    the *absolute* value the feature may take after perturbation, not the
    size of the perturbation itself.
    """

    feature_name: str
    mutable: bool
    direction: str
    min_val: float | None
    max_val: float | None


def _immutable(name: str) -> FeatureConstraint:
    return FeatureConstraint(feature_name=name, mutable=False, direction="any", min_val=None, max_val=None)


def _decreasable(name: str, min_val: float = 0.0) -> FeatureConstraint:
    """Shorthand for the common case: a non-negative "suspicion" signal that
    can only be lowered, floored at `min_val`.
    """
    return FeatureConstraint(feature_name=name, mutable=True, direction="decrease", min_val=min_val, max_val=None)


def _increasable(name: str, max_val: float | None = None) -> FeatureConstraint:
    """Shorthand for a feature that is lowered (raises suspicion) by *decreasing*,
    so the counterfactual may only raise it, capped at `max_val`.
    """
    return FeatureConstraint(feature_name=name, mutable=True, direction="increase", min_val=None, max_val=max_val)


FEATURE_CONSTRAINTS: list[FeatureConstraint] = [
    # --- Benford features (15) ---------------------------------------------
    # chi_square and mad measure deviation from Benford's Law; max_zscore is
    # the largest per-digit Z-score. All three are non-negative goodness-of-fit
    # statistics that a wallet lowers simply by trading at less artificially
    # "engineered" amounts -- there is no legitimate reason a wallet could
    # not reduce these, so they are fully mutable with a floor of 0.0.
    *(_decreasable(f"benford_chi_square_{w}") for w in ("1h", "4h", "24h", "7d", "30d")),
    *(_decreasable(f"benford_mad_{w}") for w in ("1h", "4h", "24h", "7d", "30d")),
    *(_decreasable(f"benford_max_zscore_{w}") for w in ("1h", "4h", "24h", "7d", "30d")),
    # benford_window_expanded_* are observational flags set when the Benford
    # analysis had to widen its look-back window due to sparse data. The wallet
    # cannot undo past sparsity, so these flags are immutable.
    *(_immutable(f"benford_window_expanded_{w}") for w in ("1h", "4h", "24h", "7d", "30d")),

    # --- Trade pattern features (4) -----------------------------------------
    # All four are [0, 1] ratios where a higher value is a hallmark of
    # wash-trading behaviour (concentrating volume, round-tripping, trading
    # with oneself, cancelling orders). A wallet can always choose to do
    # less of these things, so each is decreasable down to 0.0.
    _decreasable("counterparty_concentration_ratio"),
    _decreasable("round_trip_trade_frequency"),
    _decreasable("self_matching_rate"),
    _decreasable("order_cancellation_rate"),

    # --- Volume / timing features (4) ---------------------------------------
    # volume_to_unique_counterparty_ratio falls as the wallet spreads volume
    # across more counterparties (unbounded above, floored at 0.0). The
    # remaining three are [0, 1] activity ratios that fall with less
    # clustering / off-hours trading / volume spiking -- all freely reducible.
    _decreasable("volume_to_unique_counterparty_ratio"),
    _decreasable("intra_minute_clustering_coefficient"),
    _decreasable("off_hours_activity_ratio"),
    _decreasable("volume_spike_frequency"),

    # --- Wallet graph features (7) ------------------------------------------
    # funding_source_similarity_score and network_centrality are [0, 1]
    # measures of how "clustered" a wallet looks in the trading graph; a
    # wallet can reduce both by diversifying funding sources / counterparties.
    _decreasable("funding_source_similarity_score"),
    _decreasable("network_centrality"),
    # account_age_days cannot decrease -- time only moves forward.
    _immutable("account_age_days"),
    # wash_ring_membership/size/cycle_volume_ratio/timing_tightness_score
    # describe how strongly a wallet's activity matches a detected wash-ring
    # cycle; ceasing the circular/synchronised trades that define the ring
    # lowers all four.
    _decreasable("wash_ring_membership"),
    _decreasable("wash_ring_size"),
    _decreasable("cycle_volume_ratio"),
    _decreasable("timing_tightness_score"),

    # --- Cross-pair features (5) --------------------------------------------
    # All five describe how synchronised a wallet's activity is with other
    # pairs/wallets in correlated-burst windows. A wallet reduces every one
    # of them by trading fewer correlated pairs in lockstep. cross_pair_
    # synchrony_score is a Spearman r and can in principle go as low as -1.0;
    # the rest are non-negative counts/ratios floored at 0.0.
    _decreasable("cross_pair_activity_count"),
    _decreasable("cross_pair_synchrony_score", min_val=-1.0),
    _decreasable("cross_pair_burst_overlap_ratio"),
    _decreasable("shared_wallet_cluster_size"),
    _decreasable("cross_pair_volume_concentration"),

    # --- AMM pool features (3) ----------------------------------------------
    # pool_round_trip_ratio and pool_share_concentration are unambiguous
    # "more = more suspicious" pool-manipulation signals. pool_trade_ratio
    # (fraction of volume routed through pools rather than the order book) is
    # treated the same way in this model context, since it co-occurs with
    # round-tripping/share-concentration in the training signal -- a wallet
    # reduces it by routing more volume through the order book instead.
    _decreasable("pool_trade_ratio"),
    _decreasable("pool_round_trip_ratio"),
    _decreasable("pool_share_concentration"),

    # --- Path-payment features (3) ------------------------------------------
    # atomic_self_payment_ratio and path_cycle_volume_ratio directly measure
    # circular/self-dealing path payments. avg_path_hop_count is included as
    # a decreasable obfuscation signal (fewer hops = less layering); a floor
    # of 0.0 covers the no-path-payments default.
    _decreasable("atomic_self_payment_ratio"),
    _decreasable("avg_path_hop_count"),
    _decreasable("path_cycle_volume_ratio"),
    _decreasable("path_payment_frequency"),

    # --- Multi-hop path-payment cycle features (6) --------------------------
    # All six rise with cyclic self-dealing routed across separate path
    # payments, so they are only ever lowerable (floored at 0.0 for the
    # no-cycle default).
    _decreasable("path_cycle_count_24h"),
    _decreasable("path_cycle_xlm_volume_24h"),
    _decreasable("max_cycle_length"),
    _decreasable("cycle_asset_diversity"),
    _decreasable("path_cycle_count"),
    _decreasable("path_cycle_recovery_ratio"),

    # --- Sandwich-attack features (2) ---------------------------------------
    _decreasable("sandwich_ratio"),
    _decreasable("sandwich_profit_xlm_30d"),

    # --- Adversarial / evasion meta-features (6) ----------------------------
    # These target the residual signature left behind by evasion attempts
    # (over-conforming to Benford's Law, robotic timing regularity,
    # counterparty rotation, decoy trades, jitter structure) and their
    # weighted composite. All are [0, 1] suspicion scores that fall when the
    # wallet simply trades less mechanically / rotates counterparties less
    # deliberately -- none require an impossible change, so all are
    # decreasable to 0.0. evasion_composite_score is a weighted function of
    # the other five; the engine treats it as an independent feature for
    # simplicity (documented as a limitation in docs/counterfactual_explanations.md).
    _decreasable("benford_conformity_suspicion"),
    _decreasable("temporal_regularity_score"),
    _decreasable("counterparty_rotation_index"),
    _decreasable("decoy_trade_signature"),
    _decreasable("jitter_fingerprint"),
    _decreasable("evasion_composite_score"),

    # --- Cross-chain features (6) -------------------------------------------
    # has_evm_link records the historical fact that a wallet was once linked
    # to an EVM address via a bridge transfer -- that fact cannot be undone,
    # so it is immutable, exactly like account_age_days. The remaining five
    # are forward-looking behavioural ratios/medians the wallet can still
    # change going forward.
    _immutable("has_evm_link"),
    _decreasable("evm_round_trip_frequency"),
    _decreasable("evm_benford_mad_30d"),
    _decreasable("evm_counterparty_concentration"),
    _decreasable("bridge_volume_ratio"),
    # cross_chain_time_lag_median_h: a *short* lag between cross-chain legs is
    # the suspicious signature (coordinated, automated bridging); a wallet
    # reduces suspicion by waiting longer between legs, i.e. increasing this
    # feature. Capped at 720h (30 days, the longest rolling window elsewhere
    # in this codebase) as a practical upper bound on a "wait longer" ask.
    _increasable("cross_chain_time_lag_median_h", max_val=720.0),

    # --- Multivariate Benford features (3) ----------------------------------
    # benford_copula_pval is a p-value: 1.0 = no coordination evidence, so it
    # is "more suspicious when low" -- the wallet reduces suspicion by
    # *raising* it (capped at 1.0) by trading less synchronously with other
    # pairs. cross_pair_sync_ratio and digit_entropy_delta follow the usual
    # "more = more suspicious" convention and are decreasable.
    _increasable("benford_copula_pval", max_val=1.0),
    _decreasable("cross_pair_sync_ratio"),
    _decreasable("digit_entropy_delta"),

    # --- Causal (price-discovery-contribution) features (2) -----------------
    # pdc_5m/pdc_1h are signed: positive = market-making (reduces risk),
    # negative/zero = no causal price contribution (wash-trading signal). A
    # wallet reduces suspicion by *raising* its causal contribution to price
    # discovery, capped at 1.0 (the metric's natural ceiling).
    _increasable("pdc_5m", max_val=1.0),
    _increasable("pdc_1h", max_val=1.0),

    # --- T-GNN features (2) -------------------------------------------------
    # gnn_wash_ring_probability is the GNN's own [0, 1] suspicion estimate --
    # decreasable like any other suspicion score, with the same independent-
    # feature simplification noted for evasion_composite_score above.
    # gnn_neighbor_avg_score is the average risk score of the wallet's graph
    # neighbours; a wallet lowers it by trading with lower-risk counterparties.
    _decreasable("gnn_wash_ring_probability"),
    _decreasable("gnn_neighbor_avg_score"),
    # cross_chain_round_trip_score: lowering this reduces cross-chain
    # round-trip detection signal.
    _decreasable("cross_chain_round_trip_score"),
]

_missing = set(FEATURE_NAMES) - {c.feature_name for c in FEATURE_CONSTRAINTS}
if _missing:
    raise RuntimeError(f"FEATURE_CONSTRAINTS is missing entries for: {sorted(_missing)}")


def get_mutable_features() -> list[str]:
    """Return the names of all features the counterfactual engine may perturb."""
    return [c.feature_name for c in FEATURE_CONSTRAINTS if c.mutable]
