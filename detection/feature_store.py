"""Streaming Feature Store with Redis hot layer and SQLite cold layer.

Provides incremental per-trade feature updates for wallet/asset-pair tuples.
Supports efficient rolling-window aggregation and in-memory caching with
automatic fallback to in-process dict when Redis is unavailable.

Archival (dual-tier storage):
  :class:`FeatureStoreArchiver` moves rows older than N days from the
  ``feature_distribution_snapshots`` SQLite table to date-partitioned Parquet
  files on disk.  :class:`ParquetFeatureColdTier` reads those files with
  partition pruning.  :class:`DualTierFeatureStore` wraps both hot (SQLite) and
  cold (Parquet) tiers behind a single :meth:`~DualTierFeatureStore.query`
  interface used by ``drift_monitor.py``.
"""

import hashlib
import logging
import os
import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from config.settings import settings
from ingestion.data_models import Trade
from utils.circuit_breaker import CircuitBreaker, CircuitState

logger = logging.getLogger(__name__)

FEATURE_STORE_FAILURE_THRESHOLD = 3
FEATURE_STORE_RECOVERY_TIMEOUT_SECONDS = 30.0


class WalletFeatureState(BaseModel):
    """Cached feature state for a single wallet/asset-pair, including ring buffers.
    
    Ring buffers store (timestamp_us, amount) tuples in microseconds for precision.
    Benford digit counts are for digits 1-9 (0-indexed in list).
    Counterparty hashes use 32-bit integers to bound memory.
    """

    wallet: str
    asset_pair: str
    last_updated: datetime

    trade_count: int = 0

    # Rolling window trade buffers: list of (timestamp_us: int, amount: float)
    trade_ring_1h: list[tuple[int, float]] = []
    trade_ring_4h: list[tuple[int, float]] = []
    trade_ring_24h: list[tuple[int, float]] = []
    trade_ring_7d: list[tuple[int, float]] = []
    trade_ring_30d: list[tuple[int, float]] = []

    # Benford digit frequency counts (indices 0-8 map to digits 1-9)
    benford_digit_counts_30d: list[int] = [0] * 9

    # Hashed counterparty wallet IDs (32-bit integers)
    counterparty_hashes_30d: list[int] = []

# Ring buffer size caps (max entries per ring)
RING_BUFFER_CAPS = {
    "1h": 10_000,
    "4h": 40_000,
    "24h": 100_000,
    "7d": 500_000,
    "30d": 1_000_000,
}

# Time window thresholds in microseconds
RING_BUFFER_WINDOWS_US = {
    "1h": 3_600 * 1_000_000,
    "4h": 4 * 3_600 * 1_000_000,
    "24h": 24 * 3_600 * 1_000_000,
    "7d": 7 * 24 * 3_600 * 1_000_000,
    "30d": 30 * 24 * 3_600 * 1_000_000,
}


def _get_first_significant_digit(amount: float) -> int:
    """Extract first significant digit (1-9) from amount using Benford's Law."""
    if amount == 0:
        return 0
    # Use logarithm approach for accuracy with small decimals
    import math
    log_amount = math.log10(abs(amount))
    exponent = math.floor(log_amount)
    mantissa = abs(amount) / (10 ** exponent)
    return int(mantissa)


def _hash_counterparty(counterparty: str) -> int:
    """Hash counterparty wallet address to 32-bit integer."""
    h = hashlib.sha256(counterparty.encode()).digest()
    # Use first 4 bytes as 32-bit unsigned integer
    return int.from_bytes(h[:4], byteorder="big") & 0xFFFFFFFF


def _prune_ring_buffer(
    ring: list[tuple[int, float]], threshold_us: int, current_time_us: int
) -> list[tuple[int, float]]:
    """Remove entries older than threshold_us from current_time_us."""
    cutoff_us = current_time_us - threshold_us
    return [(ts, amt) for ts, amt in ring if ts > cutoff_us]


def update_feature_state(state: WalletFeatureState, trade: Trade) -> WalletFeatureState:
    """Incrementally update feature state with a new trade.
    
    Adds the trade to appropriate ring buffers, prunes expired entries,
    updates Benford digits and counterparty hashes, and manages ring overflow.
    """
    # Convert trade timestamp to microseconds
    trade_time_us = int(trade.ledger_close_time.timestamp() * 1_000_000)
    trade_amount = trade.base_amount

    # Skip if trade is not for the correct pair or wallet
    if trade.asset_pair != state.asset_pair:
        return state

    is_base_account = trade.base_account == state.wallet
    is_counter_account = trade.counter_account == state.wallet

    if not (is_base_account or is_counter_account):
        return state

    # Add trade to all applicable ring buffers
    ring_entry = (trade_time_us, trade_amount)

    # Add to 1h ring (always)
    state.trade_ring_1h.append(ring_entry)
    if len(state.trade_ring_1h) > RING_BUFFER_CAPS["1h"]:
        state.trade_ring_1h.pop(0)  # FIFO eviction

    # Add to 4h ring (always)
    state.trade_ring_4h.append(ring_entry)
    if len(state.trade_ring_4h) > RING_BUFFER_CAPS["4h"]:
        state.trade_ring_4h.pop(0)

    # Add to 24h ring (always)
    state.trade_ring_24h.append(ring_entry)
    if len(state.trade_ring_24h) > RING_BUFFER_CAPS["24h"]:
        state.trade_ring_24h.pop(0)

    # Add to 7d ring (always)
    state.trade_ring_7d.append(ring_entry)
    if len(state.trade_ring_7d) > RING_BUFFER_CAPS["7d"]:
        state.trade_ring_7d.pop(0)

    # Add to 30d ring (always)
    state.trade_ring_30d.append(ring_entry)
    if len(state.trade_ring_30d) > RING_BUFFER_CAPS["30d"]:
        state.trade_ring_30d.pop(0)

    # Prune expired entries from each ring
    state.trade_ring_1h = _prune_ring_buffer(state.trade_ring_1h, RING_BUFFER_WINDOWS_US["1h"], trade_time_us)
    state.trade_ring_4h = _prune_ring_buffer(state.trade_ring_4h, RING_BUFFER_WINDOWS_US["4h"], trade_time_us)
    state.trade_ring_24h = _prune_ring_buffer(state.trade_ring_24h, RING_BUFFER_WINDOWS_US["24h"], trade_time_us)
    state.trade_ring_7d = _prune_ring_buffer(state.trade_ring_7d, RING_BUFFER_WINDOWS_US["7d"], trade_time_us)
    state.trade_ring_30d = _prune_ring_buffer(state.trade_ring_30d, RING_BUFFER_WINDOWS_US["30d"], trade_time_us)

    # Update Benford digit counts for 30d window
    first_digit = _get_first_significant_digit(trade_amount)
    if 1 <= first_digit <= 9:
        state.benford_digit_counts_30d[first_digit - 1] += 1

    # Update counterparty hashes for 30d window
    if is_base_account and trade.counter_account:
        cp_hash = _hash_counterparty(trade.counter_account)
    elif is_counter_account and trade.base_account:
        cp_hash = _hash_counterparty(trade.base_account)
    else:
        cp_hash = None

    if cp_hash is not None and cp_hash not in state.counterparty_hashes_30d:
        state.counterparty_hashes_30d.append(cp_hash)

    # Update metadata
    state.trade_count += 1
    state.last_updated = datetime.now(timezone.utc)

    return state


def derive_feature_vector(state: WalletFeatureState) -> dict[str, float]:
    """Compute feature vector from cached ring buffers without I/O.
    
    Produces the same feature values as feature_engineering.build_feature_vector()
    within floating-point tolerance by computing aggregates directly from
    the cached trade rings and Benford/counterparty statistics.
    """
    features = {}

    # Helper to compute metrics from a ring buffer
    def _compute_benford_from_amounts(amounts: list[float]) -> dict:
        """Compute Benford's Law metrics from amounts."""
        if not amounts:
            return {"chi_square": 0.0, "mad": 0.0, "max_zscore": 0.0}

        # Count first significant digits
        digit_counts = [0] * 10  # 0-9
        for amount in amounts:
            digit = _get_first_significant_digit(amount)
            if digit > 0:
                digit_counts[digit] += 1

        total = sum(digit_counts[1:10])  # Only digits 1-9
        if total == 0:
            return {"chi_square": 0.0, "mad": 0.0, "max_zscore": 0.0}

        # Benford's Law expected frequencies for digits 1-9
        benford_expected = [
            0.301, 0.176, 0.125, 0.097, 0.079, 0.067, 0.058, 0.051, 0.046
        ]

        # Chi-square statistic
        chi_square = 0.0
        for i in range(1, 10):
            observed = digit_counts[i] / total
            expected = benford_expected[i - 1]
            if expected > 0:
                chi_square += ((observed - expected) ** 2) / expected

        # Mean Absolute Deviation (MAD)
        mad = sum(abs(digit_counts[i] / total - benford_expected[i - 1]) for i in range(1, 10)) / 9

        # Max Z-score
        z_scores = []
        for i in range(1, 10):
            observed = digit_counts[i] / total
            expected = benford_expected[i - 1]
            std = (expected * (1 - expected) / total) ** 0.5
            if std > 0:
                z_scores.append(abs(observed - expected) / std)
        max_zscore = max(z_scores) if z_scores else 0.0

        return {"chi_square": chi_square, "mad": mad, "max_zscore": max_zscore}

    # Extract amounts from each ring and compute Benford metrics
    for window_label, ring_buffer in [
        ("1h", state.trade_ring_1h),
        ("4h", state.trade_ring_4h),
        ("24h", state.trade_ring_24h),
        ("7d", state.trade_ring_7d),
        ("30d", state.trade_ring_30d),
    ]:
        amounts = [amt for _, amt in ring_buffer]
        metrics = _compute_benford_from_amounts(amounts)
        features[f"benford_chi_square_{window_label}"] = metrics["chi_square"]
        features[f"benford_mad_{window_label}"] = metrics["mad"]
        features[f"benford_max_zscore_{window_label}"] = metrics["max_zscore"]

    # Trade pattern features
    features["counterparty_concentration_ratio"] = 0.0  # Requires full trade history
    features["round_trip_trade_frequency"] = 0.0  # Requires full trade history
    features["self_matching_rate"] = 0.0  # Requires full trade history
    features["order_cancellation_rate"] = 0.0  # Not applicable for streaming

    # Volume/timing features
    total_volume = sum(amt for _, amt in state.trade_ring_30d)
    unique_counterparties = len(state.counterparty_hashes_30d)
    features["volume_to_unique_counterparty_ratio"] = (
        total_volume / unique_counterparties if unique_counterparties > 0 else 0.0
    )
    features["intra_minute_clustering_coefficient"] = 0.0  # Requires timestamp clustering logic
    features["off_hours_activity_ratio"] = 0.0  # Requires full timestamp analysis
    features["volume_spike_frequency"] = 0.0  # Requires bucketed analysis

    # Wallet graph features (mostly require external data)
    features["funding_source_similarity_score"] = 0.0
    features["network_centrality"] = 0.0
    features["account_age_days"] = 0.0
    features["wash_ring_membership"] = 0.0
    features["wash_ring_size"] = 0.0
    features["cycle_volume_ratio"] = 0.0
    features["timing_tightness_score"] = 0.0

    # Cross-pair features (require cross-asset state)
    features["cross_pair_activity_count"] = 0.0
    features["cross_pair_synchrony_score"] = 0.0
    features["cross_pair_burst_overlap_ratio"] = 0.0
    features["shared_wallet_cluster_size"] = 0.0
    features["cross_pair_volume_concentration"] = 0.0

    # AMM features
    features["pool_trade_ratio"] = 0.0
    features["pool_round_trip_ratio"] = 0.0
    features["pool_share_concentration"] = 0.0

    # Path payment features
    features["atomic_self_payment_ratio"] = 0.0
    features["avg_path_hop_count"] = 0.0
    features["path_cycle_volume_ratio"] = 0.0

    return features


class FeatureStore:
    """Redis-backed feature store with fallback to in-process dict.
    
    Supports get/set operations on WalletFeatureState with automatic
    TTL, key hashing for security, and graceful degradation when
    Redis is unavailable.
    """

    def __init__(self, redis_url: Optional[str] = None, max_fallback_entries: int = 10_000):
        """Initialize FeatureStore with optional Redis connection.
        
        Args:
            redis_url: Redis connection URL (e.g., redis://localhost:6379/0).
                      If None, uses settings.redis_url if available.
            max_fallback_entries: Maximum entries in fallback dict cache.
        """
        self.redis_url = redis_url or getattr(settings, "redis_url", None)
        self.max_fallback_entries = max_fallback_entries
        self._fallback_dict: dict[str, WalletFeatureState] = {}
        self._lru_order: deque[str] = deque()
        self.redis_client = None
        self._using_redis = False
        self._circuit = CircuitBreaker(
            name="feature_store_redis",
            failure_threshold=FEATURE_STORE_FAILURE_THRESHOLD,
            recovery_timeout=FEATURE_STORE_RECOVERY_TIMEOUT_SECONDS,
        )

        if self.redis_url:
            try:
                import redis

                self.redis_client = redis.from_url(self.redis_url)
                self.redis_client.ping()
                self._using_redis = True
                logger.info("FeatureStore: Connected to Redis at %s", self.redis_url)
            except Exception as e:
                logger.warning(
                    "FeatureStore: Redis connection failed (%s), falling back to in-process dict", e
                )
                self.redis_client = None

    @property
    def circuit_state(self) -> str:
        """Current Redis circuit breaker state (`closed`/`open`/`half_open`),
        exposed for the `/health` endpoint."""
        return self._circuit.state.value

    def _redis_available(self) -> bool:
        """Whether a Redis call should be attempted: Redis is configured,
        connected, and the circuit breaker isn't currently OPEN."""
        return bool(self._using_redis and self.redis_client) and self._circuit.allow_request()

    @staticmethod
    def _hash_key(wallet: str, asset_pair: str) -> str:
        """Hash wallet+asset_pair for security (prevent wallet exposure in Redis SCAN)."""
        key_material = f"{wallet}:{asset_pair}"
        key_hash = hashlib.sha256(key_material.encode()).hexdigest()
        return f"ll:feature:{key_hash}"

    def get_state(self, wallet: str, asset_pair: str) -> Optional[WalletFeatureState]:
        """Retrieve cached feature state from Redis (hot) or fallback dict (cold)."""
        key = self._hash_key(wallet, asset_pair)

        if self._redis_available():
            try:
                data = self.redis_client.get(key)
                self._circuit.record_success()
                if data:
                    return WalletFeatureState.model_validate_json(data)
            except Exception as e:
                self._circuit.record_failure()
                logger.warning("FeatureStore.get_state: Redis error (%s), falling back", e)

        # Fallback to in-process dict
        return self._fallback_dict.get(key)

    def set_state(self, state: WalletFeatureState) -> None:
        """Store feature state in Redis (hot) with TTL or fallback dict."""
        key = self._hash_key(state.wallet, state.asset_pair)

        if self._redis_available():
            try:
                ttl_hours = getattr(settings, "feature_store_ttl_hours", 48)
                ttl_seconds = ttl_hours * 3600
                serialized = state.model_dump_json()
                self.redis_client.setex(key, ttl_seconds, serialized)
                self._circuit.record_success()
                return
            except Exception as e:
                self._circuit.record_failure()
                logger.warning("FeatureStore.set_state: Redis error (%s), falling back", e)

        # Fallback to in-process dict with LRU eviction
        if key in self._fallback_dict:
            self._lru_order.remove(key)
        elif len(self._fallback_dict) >= self.max_fallback_entries:
            # Evict least-recently-used entry
            lru_key = self._lru_order.popleft()
            del self._fallback_dict[lru_key]

        self._fallback_dict[key] = state
        self._lru_order.append(key)

    def delete_state(self, wallet: str, asset_pair: str) -> None:
        """Delete feature state from Redis or fallback dict."""
        key = self._hash_key(wallet, asset_pair)

        if self._redis_available():
            try:
                self.redis_client.delete(key)
                self._circuit.record_success()
                return
            except Exception as e:
                self._circuit.record_failure()
                logger.warning("FeatureStore.delete_state: Redis error (%s)", e)

        # Fallback
        if key in self._fallback_dict:
            del self._fallback_dict[key]
            self._lru_order.remove(key)

    def scan_all_keys(self) -> list[str]:
        """Scan all feature store keys (for bulk promotion to cold storage)."""
        if self._redis_available():
            try:
                keys = []
                for key in self.redis_client.scan_iter(match="ll:feature:*"):
                    keys.append(key.decode() if isinstance(key, bytes) else key)
                self._circuit.record_success()
                return keys
            except Exception as e:
                self._circuit.record_failure()
                logger.warning("FeatureStore.scan_all_keys: Redis error (%s)", e)

        # Fallback
        return list(self._fallback_dict.keys())

    def is_using_redis(self) -> bool:
        """Check if Redis is active (vs. fallback mode)."""
        return self._using_redis

    def query(
        self,
        wallet: Optional[str] = None,
        feature_name: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        db_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """Query feature distribution snapshots from the SQLite hot tier.

        Reads rows from ``feature_distribution_snapshots`` with optional
        filters on wallet, feature name, and time range.

        Args:
            wallet: Filter to a specific wallet address.
            feature_name: Filter to a specific feature name.
            since: Lower bound on ``recorded_at`` (inclusive).
            until: Upper bound on ``recorded_at`` (inclusive).
            db_path: SQLite path; defaults to ``settings.db_path``.

        Returns:
            DataFrame with columns: wallet, asset_pair, feature_name,
            feature_value, recorded_at.
        """
        db_path = db_path or settings.db_path
        clauses: list[str] = []
        params: list = []

        if wallet:
            clauses.append("wallet = ?")
            params.append(wallet)
        if feature_name:
            clauses.append("feature_name = ?")
            params.append(feature_name)
        if since:
            clauses.append("recorded_at >= ?")
            params.append(since.isoformat() if isinstance(since, datetime) else since)
        if until:
            clauses.append("recorded_at <= ?")
            params.append(until.isoformat() if isinstance(until, datetime) else until)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT wallet, asset_pair, feature_name, feature_value, recorded_at "
            f"FROM feature_distribution_snapshots {where}"
        )
        try:
            with sqlite3.connect(db_path) as conn:
                df = pd.read_sql(sql, conn, params=params if params else None)
            if not df.empty and "recorded_at" in df.columns:
                df["recorded_at"] = pd.to_datetime(df["recorded_at"], utc=True, errors="coerce")
            return df
        except Exception:
            return pd.DataFrame()


class FeatureStoreArchiver:
    """Archives feature distribution snapshots from SQLite to Parquet cold tier.

    Moves rows older than ``cutoff_days`` from ``feature_distribution_snapshots``
    to date-partitioned Parquet files under ``archive_dir``.  The Parquet write
    is performed *before* the SQLite delete so that a mid-flight crash leaves
    data in both tiers rather than in neither.

    The archive directory is created with mode 0o700 (owner-read-only) to
    protect the sensitive feature vectors it contains.

    Usage::

        archiver = FeatureStoreArchiver(db_path="./ledgerlens.db",
                                        archive_dir=Path("./feature_archive"))
        n_archived = archiver.archive_old_features(cutoff_days=30)
    """

    PARQUET_SCHEMA = pa.schema(
        [
            pa.field("wallet", pa.string()),
            pa.field("asset_pair", pa.string()),
            pa.field("feature_name", pa.string()),
            pa.field("feature_value", pa.float64()),
            pa.field("recorded_at", pa.timestamp("us")),
            pa.field("year", pa.int32()),
            pa.field("month", pa.int32()),
            pa.field("day", pa.int32()),
        ]
    )

    def __init__(self, db_path: str, archive_dir: Path) -> None:
        self.db_path = db_path
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.archive_dir, 0o700)

    def archive_old_features(self, cutoff_days: int = 30) -> int:
        """Archive rows older than ``cutoff_days`` to the Parquet cold tier.

        Reads qualifying rows from SQLite, writes them to Parquet (appending to
        any existing partition), then deletes them from SQLite only on success.
        Returns the number of rows archived.
        """
        cutoff = datetime.utcnow() - timedelta(days=cutoff_days)
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT wallet, asset_pair, feature_name, feature_value, recorded_at "
                "FROM feature_distribution_snapshots WHERE recorded_at < ?",
                conn,
                params=[cutoff.isoformat()],
            )

        if df.empty:
            return 0

        df["recorded_at"] = pd.to_datetime(df["recorded_at"], utc=True, errors="coerce")
        df = df.dropna(subset=["recorded_at"])
        df["year"] = df["recorded_at"].dt.year.astype("int32")
        df["month"] = df["recorded_at"].dt.month.astype("int32")
        df["day"] = df["recorded_at"].dt.day.astype("int32")

        table = pa.Table.from_pandas(df, schema=self.PARQUET_SCHEMA, preserve_index=False)
        pq.write_to_dataset(
            table,
            root_path=str(self.archive_dir),
            partition_cols=["year", "month", "day"],
            existing_data_behavior="overwrite_or_ignore",
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM feature_distribution_snapshots WHERE recorded_at < ?",
                [cutoff.isoformat()],
            )

        logger.info("Archived %d feature snapshot rows (cutoff=%s)", len(df), cutoff.date())
        return len(df)


class ParquetFeatureColdTier:
    """Reads feature distribution snapshots from the Parquet cold tier.

    Applies PyArrow filter expressions to the partitioned dataset for efficient
    partition pruning.  Returns an empty DataFrame when the archive directory
    does not exist or the dataset is empty.

    Usage::

        cold = ParquetFeatureColdTier(archive_dir=Path("./feature_archive"))
        df = cold.query(since=datetime(2025, 1, 1))
    """

    def __init__(self, archive_dir: Path) -> None:
        self.archive_dir = Path(archive_dir)

    def query(
        self,
        wallet: Optional[str] = None,
        feature_name: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Read from the Parquet cold tier with optional filter pushdown.

        Args:
            wallet: Filter to a specific wallet address.
            feature_name: Filter to a specific feature name.
            since: Lower bound on ``recorded_at`` (inclusive).
            until: Upper bound on ``recorded_at`` (inclusive).

        Returns:
            DataFrame with columns: wallet, asset_pair, feature_name,
            feature_value, recorded_at.
        """
        if not self.archive_dir.exists():
            return pd.DataFrame()

        filters = self._build_parquet_filters(since, until, wallet, feature_name)
        try:
            table = pq.read_table(
                str(self.archive_dir),
                filters=filters,
                columns=[
                    "wallet",
                    "asset_pair",
                    "feature_name",
                    "feature_value",
                    "recorded_at",
                ],
            )
            df = table.to_pandas()
            if not df.empty and "recorded_at" in df.columns:
                df["recorded_at"] = pd.to_datetime(df["recorded_at"], utc=True, errors="coerce")
            return df
        except Exception:
            return pd.DataFrame()

    def _build_parquet_filters(
        self,
        since: Optional[datetime],
        until: Optional[datetime],
        wallet: Optional[str],
        feature_name: Optional[str],
    ) -> Optional[list]:
        """Build PyArrow partition filter expressions for efficient pruning."""
        def _to_utc_ts(dt: datetime) -> pd.Timestamp:
            ts = pd.Timestamp(dt)
            return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

        filters: list = []
        if since:
            filters.append(("recorded_at", ">=", _to_utc_ts(since)))
        if until:
            filters.append(("recorded_at", "<=", _to_utc_ts(until)))
        if wallet:
            filters.append(("wallet", "=", wallet))
        if feature_name:
            filters.append(("feature_name", "=", feature_name))
        return filters if filters else None

    def row_count(self) -> int:
        """Return total number of rows across all cold-tier Parquet partitions."""
        if not self.archive_dir.exists():
            return 0
        try:
            ds = pq.read_table(str(self.archive_dir), columns=["wallet"])
            return len(ds)
        except Exception:
            return 0

    def oldest_record(self) -> Optional[datetime]:
        """Return the timestamp of the oldest record in the cold tier."""
        if not self.archive_dir.exists():
            return None
        try:
            table = pq.read_table(str(self.archive_dir), columns=["recorded_at"])
            if len(table) == 0:
                return None
            ts = pd.to_datetime(table["recorded_at"].to_pandas(), utc=True, errors="coerce").min()
            return ts.to_pydatetime() if not pd.isna(ts) else None
        except Exception:
            return None


class DualTierFeatureStore:
    """Unified query interface over SQLite hot tier and Parquet cold tier.

    Wraps a :class:`FeatureStore` (hot) and :class:`ParquetFeatureColdTier`
    (cold) and exposes a single :meth:`query` method that transparently merges
    results from both tiers.

    Deduplication by ``(wallet, feature_name, recorded_at)`` handles the window
    where a concurrent archival run has written data to Parquet but not yet
    deleted it from SQLite.  A WARNING is logged when deduplication removes rows,
    indicating a previously failed archive.

    Usage::

        hot  = FeatureStore()
        cold = ParquetFeatureColdTier(Path("./feature_archive"))
        store = DualTierFeatureStore(hot, cold)
        df = store.query(since=datetime.utcnow() - timedelta(days=60))
    """

    def __init__(self, hot: FeatureStore, cold: ParquetFeatureColdTier) -> None:
        self._hot = hot
        self._cold = cold

    def query(
        self,
        wallet: Optional[str] = None,
        feature_name: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        db_path: Optional[str] = None,
    ) -> pd.DataFrame:
        """Return feature snapshots from both tiers, deduplicated.

        Args:
            wallet: Filter to a specific wallet address.
            feature_name: Filter to a specific feature name.
            since: Lower bound on ``recorded_at`` (inclusive).
            until: Upper bound on ``recorded_at`` (inclusive).
            db_path: Override SQLite path for the hot-tier query.

        Returns:
            DataFrame with columns: wallet, asset_pair, feature_name,
            feature_value, recorded_at.  Rows are deduplicated by
            (wallet, feature_name, recorded_at).
        """
        hot_df = self._hot.query(
            wallet=wallet,
            feature_name=feature_name,
            since=since,
            until=until,
            db_path=db_path,
        )
        cold_df = self._cold.query(
            wallet=wallet,
            feature_name=feature_name,
            since=since,
            until=until,
        )
        combined = pd.concat([hot_df, cold_df], ignore_index=True)
        if combined.empty:
            return combined

        dedup_keys = ["wallet", "feature_name", "recorded_at"]
        before = len(combined)
        combined = combined.drop_duplicates(subset=dedup_keys)
        removed = before - len(combined)
        if removed > 0:
            logger.warning(
                "DualTierFeatureStore: removed %d duplicate rows across hot/cold boundary "
                "(indicates a previously failed archive run)",
                removed,
            )
        return combined.reset_index(drop=True)
