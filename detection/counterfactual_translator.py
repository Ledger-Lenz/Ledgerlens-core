"""Human-readable translation of counterfactual feature deltas.

`detection.counterfactual_engine.generate_counterfactuals` returns raw
`{feature_name: delta}` mappings. Wallet operators are not data scientists,
so this module maps every mutable feature to a plain-English sentence
describing the on-chain behaviour change implied by reducing it. This is a
static lookup table, not an LLM -- the wording is fixed and reviewable.
"""

from detection.counterfactual_constraints import get_mutable_features

_WINDOW_LABELS = {"1h": "1-hour", "4h": "4-hour", "24h": "24-hour", "7d": "7-day", "30d": "30-day"}

_TRANSLATIONS: dict[str, str] = {}

for _window, _label in _WINDOW_LABELS.items():
    _TRANSLATIONS[f"benford_chi_square_{_window}"] = (
        f"Reduce the statistical divergence between your trade amounts' leading digits and Benford's "
        f"Law within {_label} windows -- avoid manufacturing amounts that fit an unnaturally precise "
        f"digit distribution."
    )
    _TRANSLATIONS[f"benford_mad_{_window}"] = (
        f"Make the digit distribution of your trade amounts within {_label} windows more consistent "
        f"with Benford's Law -- avoid clustering trades at psychologically round amounts."
    )
    _TRANSLATIONS[f"benford_max_zscore_{_window}"] = (
        f"Avoid concentrating trade amounts so heavily on a single leading digit within {_label} "
        f"windows that it produces a statistically extreme deviation from Benford's Law."
    )

_TRANSLATIONS.update(
    {
        "counterparty_concentration_ratio": (
            "Trade with a more diverse set of counterparties -- reduce the fraction of volume "
            "concentrated in fewer than 3 accounts."
        ),
        "round_trip_trade_frequency": (
            "Reduce the proportion of trades that are exact round-trips with the same counterparty "
            "within a 1-hour window."
        ),
        "self_matching_rate": "Stop trading against your own accounts -- route trades through independent counterparties instead.",
        "order_cancellation_rate": "Reduce how often you place and then cancel orders without execution.",
        "volume_to_unique_counterparty_ratio": (
            "Spread your trading volume across more unique counterparties rather than concentrating "
            "it on a few."
        ),
        "intra_minute_clustering_coefficient": (
            "Space out your trades so fewer of them land in the same calendar minute as other trades."
        ),
        "off_hours_activity_ratio": (
            "Shift more of your trading activity into normal market hours (currently 00:00-05:59 UTC "
            "is flagged as off-hours)."
        ),
        "volume_spike_frequency": (
            "Smooth out your trading volume over time -- avoid sudden bursts that exceed your normal "
            "hourly volume by a wide margin."
        ),
        "funding_source_similarity_score": (
            "Trade with counterparties that were funded from different sources than your own account."
        ),
        "network_centrality": (
            "Reduce how central your wallet is in the trading graph by diversifying the counterparties "
            "you trade with."
        ),
        "wash_ring_membership": "Stop trading in the circular, synchronised pattern that ties your wallet to a detected wash-trading ring.",
        "wash_ring_size": "Reduce the number of other wallets your trading is circularly linked to in a detected ring.",
        "cycle_volume_ratio": "Reduce the fraction of your volume that flows through closed circular trade cycles.",
        "timing_tightness_score": "Loosen the timing between your trades and your counterparties' -- avoid tightly synchronised execution.",
        "cross_pair_activity_count": "Reduce the number of correlated asset pairs you trade simultaneously.",
        "cross_pair_synchrony_score": "Avoid trading in close synchrony with other correlated asset pairs.",
        "cross_pair_burst_overlap_ratio": (
            "Reduce the fraction of your trades that fall inside cross-pair burst windows shared with "
            "other correlated pairs."
        ),
        "shared_wallet_cluster_size": (
            "Reduce overlap with the cluster of other wallets trading the same correlated pairs in lockstep."
        ),
        "cross_pair_volume_concentration": "Reduce the share of cross-pair burst volume attributable to your wallet.",
        "pool_trade_ratio": "Route more of your volume through the order book rather than AMM liquidity pools.",
        "pool_round_trip_ratio": "Reduce the fraction of your pool trades that are immediate round-trips.",
        "pool_share_concentration": "Reduce how concentrated your liquidity-pool share ownership is.",
        "atomic_self_payment_ratio": "Stop sending path payments where you are both the source and the destination.",
        "avg_path_hop_count": "Use simpler, more direct payment paths with fewer intermediate hops.",
        "path_cycle_volume_ratio": "Reduce the volume routed through path payments that cycle back to the same asset.",
        "path_payment_frequency": "Reduce how often you issue path payments; a high rate relative to direct trades is a layering signal.",
        "path_cycle_count_24h": "Stop routing funds through closed loops of path payments that return to your own (or associated) accounts.",
        "path_cycle_xlm_volume_24h": "Reduce the XLM value routed through multi-hop path-payment cycles that return to the originating account.",
        "max_cycle_length": "Stop chaining path payments across multiple intermediary accounts to obscure round-trip self-dealing.",
        "cycle_asset_diversity": "Stop spreading cyclic path payments across many intermediate assets to disguise round-trip trades.",
        "path_cycle_count": "Reduce the number of confirmed multi-hop round-trip path-payment cycles attributed to this wallet.",
        "path_cycle_recovery_ratio": "Avoid round-trip path payments that recover close to 100% of the originating amount, as this is a strong wash-trade signal.",
        "sandwich_ratio": "Stop placing trades immediately before and after other accounts' pool trades to capture price impact.",
        "sandwich_profit_xlm_30d": "Reduce the profit extracted from sandwiching other traders' pool trades over the last 30 days.",
        "benford_conformity_suspicion": (
            "Avoid making your trade amounts' digit distribution look artificially close to ideal "
            "Benford's Law -- natural trading is not perfectly Benford-conforming."
        ),
        "temporal_regularity_score": (
            "Introduce more natural variation into the timing between your trades instead of spacing "
            "them at regular, bot-like intervals."
        ),
        "counterparty_rotation_index": (
            "Reduce how frequently you introduce brand-new counterparties rather than trading "
            "repeatedly with an established set."
        ),
        "decoy_trade_signature": "Stop placing small low-value trades immediately before large round-trip trades.",
        "jitter_fingerprint": "Avoid adding timing jitter between trades that itself follows a fixed, detectable pattern.",
        "evasion_composite_score": (
            "Address the underlying evasion signals above (timing regularity, counterparty rotation, "
            "decoy trades, and jitter structure) -- this score is a weighted combination of those signals."
        ),
        "evm_round_trip_frequency": "Reduce the proportion of your linked EVM-chain trades that round-trip back to the same counterparty.",
        "evm_benford_mad_30d": "Make your linked EVM-chain trade amounts more consistent with Benford's Law over the last 30 days.",
        "evm_counterparty_concentration": "Trade with a more diverse set of counterparties on your linked EVM chain.",
        "bridge_volume_ratio": "Reduce the share of your volume that moves through the cross-chain bridge relative to on-chain SDEX volume.",
        "cross_chain_time_lag_median_h": "Wait longer between legs of a cross-chain transfer instead of bridging and trading back-to-back.",
        "benford_copula_pval": "Trade less synchronously with other correlated asset pairs so your joint digit distribution looks less coordinated.",
        "cross_pair_sync_ratio": "Reduce how often digit-distribution anomalies on your trades coincide in time with anomalies on other correlated pairs.",
        "digit_entropy_delta": "Avoid concentrating your trade amounts' leading digits more tightly than Benford's Law predicts across correlated pairs.",
        "pdc_5m": "Make trades that genuinely move and improve the mid-price over a 5-minute horizon, rather than trades with no causal price impact.",
        "pdc_1h": "Make trades that genuinely move and improve the mid-price over a 1-hour horizon, rather than trades with no causal price impact.",
        "gnn_wash_ring_probability": "Change your trading-graph neighbourhood and behaviour so it no longer resembles a detected wash-ring pattern.",
        "gnn_neighbor_avg_score": "Trade with counterparties that themselves have lower risk scores.",
        "path_payment_frequency": "Reduce the proportion of your trades executed as path payments to lower the wash-trading signal.",
    }
)

_missing = set(get_mutable_features()) - set(_TRANSLATIONS)
if _missing:
    raise RuntimeError(f"counterfactual_translator is missing entries for mutable features: {sorted(_missing)}")


def translate_counterfactual(deltas: dict) -> list[str]:
    """Translate a `{feature_name: delta}` mapping into human-readable English sentences.

    Every key in `deltas` must be a mutable feature with a lookup-table entry;
    raises `KeyError` otherwise.
    """
    return [_TRANSLATIONS[name] for name in deltas]
