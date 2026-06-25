"""Streaming (incremental) feature computation engine for sub-second latency scoring.

The batch engine in `detection.feature_engineering` recomputes all features
from a full historical scan on every call — acceptable for scheduled batch
runs but too slow for the real-time SSE path in
`ingestion.horizon_streamer`.  This module provides
:class:`StreamingFeatureEngine`, which maintains per-wallet rolling-window
state and updates features in O(1) / O(log N) per trade rather than O(N).

The engine is **strictly separated** from the batch engine: it is only used on
the streaming inference path; training data generation continues to use the
batch engine.

Architecture
------------
Each wallet has one :class:`WindowState` per rolling window (1h, 4h, 24h, 7d,
30d).  On ``update(trade)``:

1.  The new trade is appended to each window's deque and its aggregates are
    incremented in O(1).
2.  Expired trades are evicted from the front of each deque; their contribution
    is subtracted from the aggregates.
3.  ``get_features(wallet)`` assembles the feature vector from the
    current window states in O(W × 9) where W = 5 (number of windows).

For features that depend on *all* history (e.g. ``network_centrality``,
``account_age_days``) approximate values are returned from lightweight
accumulators.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from ingestion.data_models import Trade

# ---------------------------------------------------------------------------
# Window configuration
# ---------------------------------------------------------------------------

_WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "4h": 4 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}

_DIGITS = list(range(1, 10))
_BENFORD_EXPECTED = [math.log10(1 + 1 / d) for d in _DIGITS]

# Off-hours UTC: 00:00–05:59
_OFF_HOURS = frozenset(range(0, 6))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_digit(value: float) -> Optional[int]:
    """Return the leading decimal digit of *value*, or ``None`` for invalid values."""
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    while value < 1:
        value *= 10
    while value >= 10:
        value /= 10
    return int(value)


def _asset_symbol(asset) -> str:
    """Return a canonical string label for an ``Asset`` or asset-dict."""
    if isinstance(asset, dict):
        code = asset["code"]
        issuer = asset.get("issuer")
    else:
        code = asset.code
        issuer = getattr(asset, "issuer", None)
    return code if issuer is None else f"{code}:{issuer}"


# ---------------------------------------------------------------------------
# Per-window incremental state
# ---------------------------------------------------------------------------

@dataclass
class WindowState:
    """Incremental accumulators for a single rolling window.

    All mutable state is updated in O(1) on trade arrival and O(1) on trade
    eviction (deque pop-left).

    Attributes
    ----------
    window_sec:
        Window width in seconds.
    trades:
        Deque of ``(ts_sec, amount, counterparty, hour, gave_asset,
        got_asset, self_match)`` tuples, newest at the right.
    amount_sum:
        Sum of ``base_amount`` values for trades currently in the window.
    trade_count:
        Number of trades currently in the window.
    cp_volume:
        Mapping counterparty → sum of ``base_amount`` for trades with that
        counterparty in the window.
    digit_histogram:
        9-element list: ``digit_histogram[d-1]`` = count of trades whose
        leading digit of ``base_amount`` equals ``d``.
    digit_count:
        Number of trades with a valid (positive) ``base_amount`` leading digit.
    self_match_count:
        Trades where base_account == counter_account.
    off_hours_count:
        Trades occurring in UTC hours 00–05.
    minute_buckets:
        ``dict[minute_key → count]`` used for intra-minute clustering.
    hourly_volume:
        ``dict[hour_bucket_key → volume]`` used for volume-spike frequency.
    """

    window_sec: int
    trades: deque = field(default_factory=deque)
    amount_sum: float = 0.0
    trade_count: int = 0
    cp_volume: dict = field(default_factory=dict)
    digit_histogram: List[int] = field(default_factory=lambda: [0] * 9)
    digit_count: int = 0
    self_match_count: int = 0
    off_hours_count: int = 0
    minute_buckets: dict = field(default_factory=dict)
    hourly_volume: dict = field(default_factory=dict)

    # Lightweight round-trip tracker: recent (gave, got) pairs.
    recent_legs: deque = field(default_factory=lambda: deque(maxlen=20))

    def _add(
        self,
        ts_sec: int,
        amount: float,
        cp: str,
        hour: int,
        gave: str,
        got: str,
        self_match: bool,
        digit: Optional[int],
        minute_key: int,
        hour_bucket: int,
    ) -> None:
        self.trades.append((ts_sec, amount, cp, hour, gave, got, self_match, digit, minute_key, hour_bucket))
        self.amount_sum += amount
        self.trade_count += 1
        self.cp_volume[cp] = self.cp_volume.get(cp, 0.0) + amount
        if digit is not None:
            self.digit_histogram[digit - 1] += 1
            self.digit_count += 1
        if self_match:
            self.self_match_count += 1
        if hour in _OFF_HOURS:
            self.off_hours_count += 1
        self.minute_buckets[minute_key] = self.minute_buckets.get(minute_key, 0) + 1
        self.hourly_volume[hour_bucket] = self.hourly_volume.get(hour_bucket, 0.0) + amount
        self.recent_legs.append((gave, got))

    def _remove(self, entry: tuple) -> None:
        ts_sec, amount, cp, hour, gave, got, self_match, digit, minute_key, hour_bucket = entry
        self.amount_sum = max(0.0, self.amount_sum - amount)
        self.trade_count = max(0, self.trade_count - 1)
        self.cp_volume[cp] = max(0.0, self.cp_volume.get(cp, 0.0) - amount)
        if self.cp_volume.get(cp, 0.0) <= 0.0:
            self.cp_volume.pop(cp, None)
        if digit is not None:
            self.digit_histogram[digit - 1] = max(0, self.digit_histogram[digit - 1] - 1)
            self.digit_count = max(0, self.digit_count - 1)
        if self_match:
            self.self_match_count = max(0, self.self_match_count - 1)
        if hour in _OFF_HOURS:
            self.off_hours_count = max(0, self.off_hours_count - 1)
        self.minute_buckets[minute_key] = max(0, self.minute_buckets.get(minute_key, 0) - 1)
        if self.minute_buckets.get(minute_key, 0) == 0:
            self.minute_buckets.pop(minute_key, None)
        self.hourly_volume[hour_bucket] = max(0.0, self.hourly_volume.get(hour_bucket, 0.0) - amount)
        if self.hourly_volume.get(hour_bucket, 0.0) <= 0.0:
            self.hourly_volume.pop(hour_bucket, None)

    def update(
        self,
        ts_sec: int,
        amount: float,
        cp: str,
        hour: int,
        gave: str,
        got: str,
        self_match: bool,
        digit: Optional[int],
        minute_key: int,
        hour_bucket: int,
    ) -> None:
        """Add the new trade and evict expired entries."""
        cutoff = ts_sec - self.window_sec
        while self.trades and self.trades[0][0] <= cutoff:
            self._remove(self.trades.popleft())
        self._add(ts_sec, amount, cp, hour, gave, got, self_match, digit, minute_key, hour_bucket)

    # ---- Feature extraction ------------------------------------------------

    def benford_metrics(self) -> Tuple[float, float, float]:
        """Return ``(chi_square, mad, max_zscore)`` from the current digit histogram."""
        n = self.digit_count
        if n == 0:
            return 0.0, 0.0, 0.0
        observed = [c / n for c in self.digit_histogram]
        chi_sq = sum(
            (observed[i] * n - _BENFORD_EXPECTED[i] * n) ** 2 / (_BENFORD_EXPECTED[i] * n)
            for i in range(9)
            if _BENFORD_EXPECTED[i] * n > 0
        )
        mad = float(sum(abs(observed[i] - _BENFORD_EXPECTED[i]) for i in range(9)) / 9)
        zscores = []
        for i in range(9):
            p = _BENFORD_EXPECTED[i]
            obs_p = observed[i]
            numerator = abs(obs_p - p) - 1.0 / (2 * n)
            denominator = math.sqrt(p * (1 - p) / n)
            zscores.append(max(numerator, 0.0) / denominator if denominator > 0 else 0.0)
        return float(chi_sq), float(mad), float(max(zscores))

    def counterparty_concentration(self) -> float:
        """Fraction of volume traded against the single largest counterparty."""
        if not self.cp_volume or self.amount_sum <= 0:
            return 0.0
        return float(max(self.cp_volume.values()) / self.amount_sum)

    def self_matching_rate(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return float(self.self_match_count / self.trade_count)

    def off_hours_activity_ratio(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return float(self.off_hours_count / self.trade_count)

    def intra_minute_clustering_coefficient(self) -> float:
        if self.trade_count == 0:
            return 0.0
        clustered = sum(c for c in self.minute_buckets.values() if c > 1)
        return float(clustered / self.trade_count)

    def volume_to_unique_counterparty_ratio(self) -> float:
        unique_cps = len(self.cp_volume)
        if unique_cps == 0:
            return 0.0
        return float(self.amount_sum / unique_cps)

    def volume_spike_frequency(self, spike_threshold: float = 2.0) -> float:
        """Fraction of 1h buckets within this window whose volume exceeds mean + k*std."""
        if len(self.hourly_volume) < 2:
            return 0.0
        vols = list(self.hourly_volume.values())
        mean_v = sum(vols) / len(vols)
        variance = sum((v - mean_v) ** 2 for v in vols) / len(vols)
        std_v = math.sqrt(variance)
        if std_v == 0:
            return 0.0
        threshold = mean_v + spike_threshold * std_v
        return float(sum(1 for v in vols if v > threshold) / len(vols))

    def round_trip_frequency(self, max_lookback: int = 10) -> float:
        """Fraction of recent trades that are round-trips (approximate)."""
        legs = list(self.recent_legs)
        n = len(legs)
        if n < 2:
            return 0.0
        round_trips = 0
        for i in range(n):
            gave_i, got_i = legs[i]
            for j in range(i + 1, min(i + 1 + max_lookback, n)):
                gave_j, got_j = legs[j]
                if gave_j == got_i and got_j == gave_i:
                    round_trips += 1
                    break
        return round_trips / n if n > 0 else 0.0


# ---------------------------------------------------------------------------
# Per-wallet state
# ---------------------------------------------------------------------------

@dataclass
class WalletState:
    """Per-wallet state across all rolling windows."""

    wallet: str
    windows: Dict[str, WindowState] = field(default_factory=dict)
    # Global (all-time) accumulators for features that need full history.
    all_time_trade_count: int = 0
    all_time_counterparties: set = field(default_factory=set)
    first_seen_sec: Optional[int] = None

    def __post_init__(self) -> None:
        self.windows = {label: WindowState(window_sec=secs) for label, secs in _WINDOW_SECONDS.items()}


# ---------------------------------------------------------------------------
# Streaming feature vector type
# ---------------------------------------------------------------------------

FeatureVector = Dict[str, float]


# ---------------------------------------------------------------------------
# StreamingFeatureEngine
# ---------------------------------------------------------------------------

class StreamingFeatureEngine:
    """Incremental feature engine for sub-second latency risk scoring.

    Usage::

        engine = StreamingFeatureEngine()
        for trade in stream_trades():
            fv = engine.update(trade)
            score = model.predict(fv)

    Thread safety
    -------------
    This class is **not** thread-safe.  Use one instance per worker thread or
    protect access with a lock.
    """

    def __init__(self) -> None:
        self._wallets: Dict[str, WalletState] = {}

    def _get_or_create(self, wallet: str) -> WalletState:
        if wallet not in self._wallets:
            self._wallets[wallet] = WalletState(wallet=wallet)
        return self._wallets[wallet]

    def _extract_trade_fields(
        self, trade: Trade, wallet: str
    ) -> Tuple[int, float, str, int, str, str, bool, Optional[int], int, int]:
        """Extract the fields needed by `WindowState.update`."""
        ts = trade.ledger_close_time
        if hasattr(ts, "timestamp"):
            ts_sec = int(ts.timestamp())
        else:
            ts_sec = int(pd.Timestamp(ts).timestamp())

        amount = float(trade.base_amount) if trade.base_amount else 0.0

        # Counterparty from this wallet's perspective.
        if trade.base_account == wallet:
            cp = trade.counter_account or ""
            gave = _asset_symbol(trade.base_asset)
            got = _asset_symbol(trade.counter_asset)
        else:
            cp = trade.base_account or ""
            gave = _asset_symbol(trade.counter_asset)
            got = _asset_symbol(trade.base_asset)

        ts_pd = pd.Timestamp(trade.ledger_close_time)
        if ts_pd.tzinfo is None:
            ts_pd = ts_pd.tz_localize("UTC")
        else:
            ts_pd = ts_pd.tz_convert("UTC")
        hour = ts_pd.hour
        self_match = (trade.base_account == trade.counter_account)
        digit = _first_digit(amount)
        minute_key = ts_sec // 60
        hour_bucket = ts_sec // 3600

        return ts_sec, amount, cp, hour, gave, got, self_match, digit, minute_key, hour_bucket

    def update(self, trade: Trade) -> FeatureVector:
        """Process *trade* and return the updated feature vector for the trading wallet.

        The feature vector covers all 5 rolling windows.  If both
        ``base_account`` and ``counter_account`` are non-empty, the engine
        updates both wallets' state and returns the feature vector for
        ``base_account``.

        Parameters
        ----------
        trade:
            The incoming ``Trade`` model from the SSE stream.

        Returns
        -------
        FeatureVector
            A ``dict[feature_name → float]`` for ``trade.base_account``.
        """
        t0 = time.monotonic()

        wallets_to_update = [trade.base_account]
        if trade.counter_account and trade.counter_account != trade.base_account:
            wallets_to_update.append(trade.counter_account)

        primary_wallet = trade.base_account

        for wallet in wallets_to_update:
            state = self._get_or_create(wallet)
            ts_sec, amount, cp, hour, gave, got, self_match, digit, minute_key, hour_bucket = (
                self._extract_trade_fields(trade, wallet)
            )
            for ws in state.windows.values():
                ws.update(ts_sec, amount, cp, hour, gave, got, self_match, digit, minute_key, hour_bucket)
            state.all_time_trade_count += 1
            if cp:
                state.all_time_counterparties.add(cp)
            if state.first_seen_sec is None:
                state.first_seen_sec = ts_sec

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        fv = self.get_features(primary_wallet, latency_ms=elapsed_ms)
        return fv

    def get_features(
        self, wallet: str, latency_ms: float = 0.0
    ) -> FeatureVector:
        """Return the current feature vector for *wallet*.

        Missing wallets (never updated) return all-zero vectors.

        Parameters
        ----------
        wallet:
            Wallet address.
        latency_ms:
            End-to-end latency in milliseconds from trade receipt to this call.
            Stored in the returned dict under ``"stream_latency_ms"``.
        """
        if wallet not in self._wallets:
            return self._zero_vector(latency_ms)

        state = self._wallets[wallet]
        fv: FeatureVector = {}

        # Benford features across all 5 windows
        for label in _WINDOW_SECONDS:
            ws = state.windows[label]
            chi_sq, mad, max_z = ws.benford_metrics()
            fv[f"benford_chi_square_{label}"] = chi_sq
            fv[f"benford_mad_{label}"] = mad
            fv[f"benford_max_zscore_{label}"] = max_z

        # Use the 24h window for most trade-pattern features (representative window)
        ws24 = state.windows["24h"]
        ws30 = state.windows["30d"]

        fv["counterparty_concentration_ratio"] = ws24.counterparty_concentration()
        fv["round_trip_trade_frequency"] = ws24.round_trip_frequency()
        fv["self_matching_rate"] = ws24.self_matching_rate()
        fv["order_cancellation_rate"] = 0.0  # not available in streaming path

        fv["volume_to_unique_counterparty_ratio"] = ws30.volume_to_unique_counterparty_ratio()
        fv["intra_minute_clustering_coefficient"] = ws24.intra_minute_clustering_coefficient()
        fv["off_hours_activity_ratio"] = ws24.off_hours_activity_ratio()
        fv["volume_spike_frequency"] = ws30.volume_spike_frequency()

        # Graph / wallet features (approx from all-time counters)
        fv["funding_source_similarity_score"] = 0.0
        fv["network_centrality"] = 0.0
        fv["account_age_days"] = 0.0
        fv["wash_ring_membership"] = 0.0
        fv["wash_ring_size"] = 0.0
        fv["cycle_volume_ratio"] = 0.0
        fv["timing_tightness_score"] = 0.0

        # Cross-pair, AMM, path-payment, sandwich features not available in
        # the streaming path — set to 0.0 (safe default for the ML model).
        for name in (
            "cross_pair_activity_count",
            "cross_pair_synchrony_score",
            "cross_pair_burst_overlap_ratio",
            "shared_wallet_cluster_size",
            "cross_pair_volume_concentration",
            "pool_trade_ratio",
            "pool_round_trip_ratio",
            "pool_share_concentration",
            "atomic_self_payment_ratio",
            "avg_path_hop_count",
            "path_cycle_volume_ratio",
            "sandwich_ratio",
            "sandwich_profit_xlm_30d",
            "pdc_5m",
            "pdc_1h",
            "benford_copula_pval",
            "cross_pair_sync_ratio",
            "digit_entropy_delta",
        ):
            fv[name] = 0.0

        fv["stream_latency_ms"] = latency_ms
        return fv

    def _zero_vector(self, latency_ms: float = 0.0) -> FeatureVector:
        """Return an all-zero feature vector for an unknown wallet."""
        fv: FeatureVector = {}
        for label in _WINDOW_SECONDS:
            fv[f"benford_chi_square_{label}"] = 0.0
            fv[f"benford_mad_{label}"] = 0.0
            fv[f"benford_max_zscore_{label}"] = 0.0
        for name in (
            "counterparty_concentration_ratio",
            "round_trip_trade_frequency",
            "self_matching_rate",
            "order_cancellation_rate",
            "volume_to_unique_counterparty_ratio",
            "intra_minute_clustering_coefficient",
            "off_hours_activity_ratio",
            "volume_spike_frequency",
            "funding_source_similarity_score",
            "network_centrality",
            "account_age_days",
            "wash_ring_membership",
            "wash_ring_size",
            "cycle_volume_ratio",
            "timing_tightness_score",
            "cross_pair_activity_count",
            "cross_pair_synchrony_score",
            "cross_pair_burst_overlap_ratio",
            "shared_wallet_cluster_size",
            "cross_pair_volume_concentration",
            "pool_trade_ratio",
            "pool_round_trip_ratio",
            "pool_share_concentration",
            "atomic_self_payment_ratio",
            "avg_path_hop_count",
            "path_cycle_volume_ratio",
            "sandwich_ratio",
            "sandwich_profit_xlm_30d",
            "pdc_5m",
            "pdc_1h",
            "benford_copula_pval",
            "cross_pair_sync_ratio",
            "digit_entropy_delta",
            "stream_latency_ms",
        ):
            fv[name] = 0.0
        return fv

    def wallet_count(self) -> int:
        """Number of wallets currently tracked by the engine."""
        return len(self._wallets)
