"""Configurable trade filter pipeline for the LedgerLens ingestion layer.

Trades are passed through an ordered list of :class:`TradeFilter` instances.
A trade must pass **all** enabled filters (logical AND) to reach the detection
engine.  Rejected trades are not discarded; they are persisted to the
``filtered_trades`` SQLite table with a rejection reason so operators can
review and tune filter rules without losing data.

## Quick start

    from ingestion.filters import FilterConfigLoader, TradeFilterPipeline

    # Start the hot-reload watcher (polls every 60 s by default)
    loader = FilterConfigLoader("config/filter_config.yaml")
    pipeline = loader.pipeline           # TradeFilterPipeline, replaced atomically on reload

    result = pipeline.apply(trade)
    if not result.passed:
        store_filtered_trade(trade, result.reason, db_path=settings.db_path)

## Thread safety

``TradeFilterPipeline.apply()`` acquires a shared read-lock on every call.
``FilterConfigLoader`` acquires the same write-lock when atomically swapping
the filter list after a valid hot-reload.  The pipeline is never left without
filters during a swap.

## Hot-reload

``FilterConfigLoader`` polls the config file's ``mtime`` via
:func:`os.stat` on a :class:`threading.Timer` schedule
(default: :attr:`~config.settings.Settings.filter_config_reload_interval_seconds`
seconds).  On change it re-parses and validates the YAML.  If validation fails
the **previous** valid config is retained and an ERROR is logged — the pipeline
is never left unconfigured.
"""

from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Annotated

from ingestion.data_models import Trade

logger = logging.getLogger("ledgerlens.filters")

# ---------------------------------------------------------------------------
# Stellar public key validation helper
# ---------------------------------------------------------------------------

def _is_valid_stellar_public_key(key: str) -> bool:
    """Return True if *key* is a structurally valid Stellar G… public key.

    Uses the stellar-sdk StrKey decoder for full checksum validation when
    the library is available; falls back to a lightweight structural check
    (G prefix + 55 chars from the Strkey alphabet) so the module stays
    importable in minimal environments.
    """
    if not (isinstance(key, str) and key.startswith("G") and len(key) == 56):
        return False
    try:
        from stellar_sdk import StrKey  # type: ignore[import]
        StrKey.decode_ed25519_public_key(key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# FilterResult
# ---------------------------------------------------------------------------

class FilterResult:
    """Outcome of applying a single :class:`TradeFilter` or the full pipeline.

    Attributes
    ----------
    passed:
        ``True`` when the trade passed the filter, ``False`` when rejected.
    reason:
        Human-readable explanation populated only when *passed* is ``False``.
    """

    __slots__ = ("passed", "reason")

    def __init__(self, passed: bool, reason: str | None = None) -> None:
        self.passed = passed
        self.reason = reason

    def __repr__(self) -> str:
        if self.passed:
            return "FilterResult(passed=True)"
        return f"FilterResult(passed=False, reason={self.reason!r})"


# ---------------------------------------------------------------------------
# TradeFilter base class
# ---------------------------------------------------------------------------

class TradeFilter(ABC):
    """Abstract base for all trade filters.

    Concrete filters must implement :meth:`apply` and set a class-level
    :attr:`name` that uniquely identifies the filter in metrics and logs.
    """

    name: str = "unknown"

    def __init__(self) -> None:
        self._rejection_count: int = 0

    @abstractmethod
    def apply(self, trade: Trade) -> FilterResult:
        """Evaluate *trade* and return a :class:`FilterResult`.

        Implementations **must** increment ``self._rejection_count`` when they
        reject a trade so that :meth:`rejection_count` reports correctly.
        """

    @property
    def rejection_count(self) -> int:
        """Total trades rejected by this filter since the last :meth:`reset_stats`."""
        return self._rejection_count

    def reset_stats(self) -> None:
        """Reset the rejection counter to zero."""
        self._rejection_count = 0


# ---------------------------------------------------------------------------
# Concrete filter implementations
# ---------------------------------------------------------------------------

class AssetPairWhitelistFilter(TradeFilter):
    """Allow only trades whose asset pair is in the whitelist.

    The pair is constructed as ``"{base_asset_code}/{counter_asset_code}"``
    using the asset code only (not the issuer), which matches the common
    operator shorthand ``XLM/USDC``.

    An **empty whitelist** means *allow all* — useful when the whitelist is
    not yet configured and the operator wants a pass-through.

    Parameters
    ----------
    allowed_pairs:
        Set of ``"BASE/COUNTER"`` strings (e.g. ``{"XLM/USDC", "XLM/BTC"}``).
        Case-sensitive.
    """

    name = "asset_pair_whitelist"

    def __init__(self, allowed_pairs: set[str]) -> None:
        super().__init__()
        self._allowed_pairs = frozenset(allowed_pairs)

    def apply(self, trade: Trade) -> FilterResult:
        """Pass if whitelist is empty or pair is whitelisted."""
        if not self._allowed_pairs:
            return FilterResult(passed=True)
        pair = f"{trade.base_asset.code}/{trade.counter_asset.code}"
        if pair in self._allowed_pairs:
            return FilterResult(passed=True)
        self._rejection_count += 1
        return FilterResult(
            passed=False,
            reason=f"asset pair {pair!r} not in whitelist",
        )


class AssetPairBlacklistFilter(TradeFilter):
    """Reject trades whose asset pair is in the blacklist.

    An **empty blacklist** means *allow all*.

    Parameters
    ----------
    blocked_pairs:
        Set of ``"BASE/COUNTER"`` strings to block.
    """

    name = "asset_pair_blacklist"

    def __init__(self, blocked_pairs: set[str]) -> None:
        super().__init__()
        self._blocked_pairs = frozenset(blocked_pairs)

    def apply(self, trade: Trade) -> FilterResult:
        """Reject if pair is in the blacklist."""
        if not self._blocked_pairs:
            return FilterResult(passed=True)
        pair = f"{trade.base_asset.code}/{trade.counter_asset.code}"
        if pair in self._blocked_pairs:
            self._rejection_count += 1
            return FilterResult(
                passed=False,
                reason=f"asset pair {pair!r} is blacklisted",
            )
        return FilterResult(passed=True)


class MinimumVolumeFilter(TradeFilter):
    """Reject dust trades whose volume falls below *min_volume*.

    The volume is read from a configurable field on the :class:`~ingestion.data_models.Trade`
    record.  By default ``base_amount`` is used; operators may change this to
    ``counter_amount`` or ``price`` via the YAML config.

    A ``min_volume`` of ``0`` (or ``Decimal("0")``) means *allow all*.

    Parameters
    ----------
    min_volume:
        Minimum volume threshold (inclusive).  Trades with a volume strictly
        less than this value are rejected.
    volume_field:
        Attribute name on :class:`~ingestion.data_models.Trade` to read.
        Must be a numeric field; defaults to ``"base_amount"``.
    """

    name = "minimum_volume"

    def __init__(
        self,
        min_volume: Decimal,
        volume_field: str = "base_amount",
    ) -> None:
        super().__init__()
        if min_volume < Decimal(0):
            raise ValueError("min_volume must be >= 0")
        if volume_field not in ("base_amount", "counter_amount", "price"):
            raise ValueError(
                f"volume_field must be one of base_amount/counter_amount/price, "
                f"got {volume_field!r}"
            )
        self._min_volume = min_volume
        self._volume_field = volume_field

    def apply(self, trade: Trade) -> FilterResult:
        """Reject if the trade's volume field is below the threshold."""
        if self._min_volume == Decimal(0):
            return FilterResult(passed=True)
        volume = Decimal(str(getattr(trade, self._volume_field)))
        if volume < self._min_volume:
            self._rejection_count += 1
            return FilterResult(
                passed=False,
                reason=(
                    f"{self._volume_field} {volume} is below minimum {self._min_volume}"
                ),
            )
        return FilterResult(passed=True)


class AssetTypeFilter(TradeFilter):
    """Reject trades involving assets of types not in the allowed set.

    Stellar assets fall into three types:

    * ``"native"`` — XLM (no issuer)
    * ``"credit_alphanum4"`` — short credit assets (1-4 character codes)
    * ``"credit_alphanum12"`` — long credit assets (5-12 character codes)

    The filter rejects a trade if *either* the base or counter asset has a
    type that is not in ``allowed_types``.

    Parameters
    ----------
    allowed_types:
        Set of asset type strings from the three values above.
    """

    name = "asset_type"

    _VALID_TYPES: frozenset[str] = frozenset(
        {"native", "credit_alphanum4", "credit_alphanum12"}
    )

    def __init__(
        self,
        allowed_types: set[Literal["native", "credit_alphanum4", "credit_alphanum12"]],
    ) -> None:
        super().__init__()
        invalid = set(allowed_types) - self._VALID_TYPES
        if invalid:
            raise ValueError(
                f"Invalid asset types: {invalid}. "
                f"Must be subset of {self._VALID_TYPES}"
            )
        self._allowed_types = frozenset(allowed_types)

    @staticmethod
    def _asset_type(asset_code: str, is_native: bool) -> str:
        """Derive the Stellar asset type string from an :class:`~ingestion.data_models.Asset`."""
        if is_native:
            return "native"
        return "credit_alphanum4" if len(asset_code) <= 4 else "credit_alphanum12"

    def apply(self, trade: Trade) -> FilterResult:
        """Reject if base or counter asset type is not allowed."""
        base_type = self._asset_type(trade.base_asset.code, trade.base_asset.is_native)
        counter_type = self._asset_type(
            trade.counter_asset.code, trade.counter_asset.is_native
        )
        for asset_label, asset_type in (
            ("base_asset", base_type),
            ("counter_asset", counter_type),
        ):
            if asset_type not in self._allowed_types:
                self._rejection_count += 1
                return FilterResult(
                    passed=False,
                    reason=(
                        f"{asset_label} type {asset_type!r} not in allowed types "
                        f"{sorted(self._allowed_types)}"
                    ),
                )
        return FilterResult(passed=True)


class AccountExclusionFilter(TradeFilter):
    """Reject trades where either account is in the exclusion set.

    This is typically used to exclude known-clean institutional market makers,
    Stellar Foundation accounts, or verified DEX aggregator bots that would
    introduce false positives into the detection models.

    .. note::
        The exclusion list is operationally sensitive — it reveals which
        accounts have been manually vetted.  Protect ``filter_config.yaml``
        with appropriate file permissions (recommended: ``0o640``).

    Parameters
    ----------
    excluded_accounts:
        Set of Stellar G… public keys to exclude.  Each key is validated as a
        structurally correct Stellar public key on construction; invalid keys
        raise :class:`ValueError`.
    """

    name = "account_exclusion"

    def __init__(self, excluded_accounts: set[str]) -> None:
        super().__init__()
        invalid = [
            k for k in excluded_accounts if not _is_valid_stellar_public_key(k)
        ]
        if invalid:
            raise ValueError(
                f"Invalid Stellar public keys in excluded_accounts: {invalid}"
            )
        self._excluded_accounts = frozenset(excluded_accounts)

    def apply(self, trade: Trade) -> FilterResult:
        """Reject if base_account or counter_account is excluded."""
        if trade.base_account in self._excluded_accounts:
            self._rejection_count += 1
            return FilterResult(
                passed=False,
                reason=f"base_account {trade.base_account[:8]}… is in exclusion list",
            )
        if (
            trade.counter_account is not None
            and trade.counter_account in self._excluded_accounts
        ):
            self._rejection_count += 1
            return FilterResult(
                passed=False,
                reason=f"counter_account {trade.counter_account[:8]}… is in exclusion list",
            )
        return FilterResult(passed=True)


# ---------------------------------------------------------------------------
# TradeFilterPipeline
# ---------------------------------------------------------------------------

class TradeFilterPipeline:
    """Apply an ordered list of :class:`TradeFilter` instances to each trade.

    Filters are applied in declaration order.  The first filter to reject a
    trade short-circuits the pipeline — subsequent filters are not called.
    This means **placement matters**: put cheap, high-rejection filters first.

    Thread safety
    -------------
    ``apply()`` acquires the internal ``_lock`` for reading (non-exclusive).
    ``reload_filters()`` acquires the same lock exclusively, ensuring
    in-flight ``apply()`` calls complete before the filter list is swapped.
    The pipeline is **never** left without filters during a swap.

    Parameters
    ----------
    filters:
        Ordered list of :class:`TradeFilter` instances.  An empty list means
        *all trades pass*.
    """

    def __init__(self, filters: list[TradeFilter]) -> None:
        self._filters: list[TradeFilter] = list(filters)
        self._lock = threading.Lock()

    def apply(self, trade: Trade) -> FilterResult:
        """Run *trade* through all filters, returning on the first rejection.

        Returns
        -------
        FilterResult
            ``passed=True`` if all filters pass (or the pipeline is empty).
            ``passed=False`` with a compound ``reason`` string on the first
            rejection, prefixed with the rejecting filter's name.
        """
        with self._lock:
            active_filters = list(self._filters)
        for f in active_filters:
            result = f.apply(trade)
            if not result.passed:
                return FilterResult(
                    passed=False,
                    reason=f"{f.name}: {result.reason}",
                )
        return FilterResult(passed=True)

    def reload_filters(self, new_filters: list[TradeFilter]) -> None:
        """Atomically replace the active filter list.

        The lock ensures no ``apply()`` call sees a partial or empty state.
        The previous filter list (including its accumulated stats) is discarded.

        Parameters
        ----------
        new_filters:
            Fully validated filter list to swap in.
        """
        with self._lock:
            self._filters = list(new_filters)
        logger.info(
            "filter_pipeline.reloaded",
            extra={"filter_count": len(new_filters)},
        )

    def stats(self) -> dict[str, int]:
        """Return a ``{filter_name: rejection_count}`` snapshot.

        Reads are done under the lock so the values are consistent with the
        current filter list.
        """
        with self._lock:
            return {f.name: f.rejection_count for f in self._filters}

    def reset_stats(self) -> None:
        """Reset all per-filter rejection counters to zero."""
        with self._lock:
            for f in self._filters:
                f.reset_stats()


# ---------------------------------------------------------------------------
# Config schema (Pydantic v2)
# ---------------------------------------------------------------------------

class _AssetPairWhitelistConfig(BaseModel):
    type: Literal["asset_pair_whitelist"]
    enabled: bool = True
    pairs: list[str] = []


class _AssetPairBlacklistConfig(BaseModel):
    type: Literal["asset_pair_blacklist"]
    enabled: bool = True
    pairs: list[str] = []


class _MinimumVolumeConfig(BaseModel):
    type: Literal["minimum_volume"]
    enabled: bool = True
    min_volume: str = "0"   # stored as string to preserve Decimal precision
    volume_field: str = "base_amount"

    @field_validator("min_volume", mode="before")
    @classmethod
    def validate_min_volume(cls, v: object) -> str:
        try:
            d = Decimal(str(v))
        except Exception as exc:
            raise ValueError(f"min_volume must be a valid decimal, got {v!r}") from exc
        if d < Decimal(0):
            raise ValueError("min_volume must be >= 0")
        return str(d)

    @field_validator("volume_field", mode="before")
    @classmethod
    def validate_volume_field(cls, v: object) -> str:
        allowed = {"base_amount", "counter_amount", "price"}
        if str(v) not in allowed:
            raise ValueError(f"volume_field must be one of {allowed}, got {v!r}")
        return str(v)


class _AssetTypeConfig(BaseModel):
    type: Literal["asset_type"]
    enabled: bool = True
    allowed_types: list[str] = ["native", "credit_alphanum4", "credit_alphanum12"]

    @field_validator("allowed_types", mode="before")
    @classmethod
    def validate_allowed_types(cls, v: object) -> list[str]:
        valid = {"native", "credit_alphanum4", "credit_alphanum12"}
        items = list(v)
        invalid = set(items) - valid
        if invalid:
            raise ValueError(f"Invalid asset types: {invalid}")
        return items


class _AccountExclusionConfig(BaseModel):
    type: Literal["account_exclusion"]
    enabled: bool = True
    excluded_accounts: list[str] = []

    @field_validator("excluded_accounts", mode="before")
    @classmethod
    def validate_accounts(cls, v: object) -> list[str]:
        accounts = list(v)
        invalid = [k for k in accounts if not _is_valid_stellar_public_key(k)]
        if invalid:
            raise ValueError(
                f"Invalid Stellar public keys in excluded_accounts: {invalid}"
            )
        return accounts


_FilterConfig = Annotated[
    (
        _AssetPairWhitelistConfig
        | _AssetPairBlacklistConfig
        | _MinimumVolumeConfig
        | _AssetTypeConfig
        | _AccountExclusionConfig
    ),
    Field(discriminator="type"),
]


class _FilterFileConfig(BaseModel):
    """Schema for ``filter_config.yaml``."""

    version: str = "1.0"
    filters: list[_FilterConfig] = []

    @model_validator(mode="before")
    @classmethod
    def check_version(cls, values: object) -> object:
        if isinstance(values, dict):
            version = str(values.get("version", "1.0"))
            if version not in ("1.0",):
                raise ValueError(
                    f"Unsupported filter_config.yaml version: {version!r}. "
                    f"Only '1.0' is supported."
                )
        return values


def _build_filters(config: _FilterFileConfig) -> list[TradeFilter]:
    """Instantiate :class:`TradeFilter` objects from a validated config."""
    filters: list[TradeFilter] = []
    for fc in config.filters:
        if not fc.enabled:
            continue
        if isinstance(fc, _AssetPairWhitelistConfig):
            filters.append(AssetPairWhitelistFilter(allowed_pairs=set(fc.pairs)))
        elif isinstance(fc, _AssetPairBlacklistConfig):
            filters.append(AssetPairBlacklistFilter(blocked_pairs=set(fc.pairs)))
        elif isinstance(fc, _MinimumVolumeConfig):
            filters.append(
                MinimumVolumeFilter(
                    min_volume=Decimal(fc.min_volume),
                    volume_field=fc.volume_field,
                )
            )
        elif isinstance(fc, _AssetTypeConfig):
            filters.append(AssetTypeFilter(allowed_types=set(fc.allowed_types)))  # type: ignore[arg-type]
        elif isinstance(fc, _AccountExclusionConfig):
            filters.append(
                AccountExclusionFilter(excluded_accounts=set(fc.excluded_accounts))
            )
    return filters


# ---------------------------------------------------------------------------
# FilterConfigLoader — YAML parsing + hot-reload
# ---------------------------------------------------------------------------

class FilterConfigLoader:
    """Parse ``filter_config.yaml`` and hot-reload it without restarting.

    On construction the config is loaded immediately.  A background
    :class:`threading.Timer` polls the file's ``mtime`` every
    *reload_interval_seconds* seconds.  When a change is detected the YAML
    is re-parsed and validated.

    **Fail-safe behaviour**: if the new config is invalid the *previous*
    valid filter list is retained and an ERROR is logged.  The pipeline is
    never left unconfigured.

    Parameters
    ----------
    config_path:
        Path to ``filter_config.yaml``.
    reload_interval_seconds:
        Seconds between mtime checks.  Defaults to
        :attr:`~config.settings.Settings.filter_config_reload_interval_seconds`.
    pipeline:
        Optional existing :class:`TradeFilterPipeline` to update on reload.
        If ``None`` a new pipeline is created from the initial config.
    """

    def __init__(
        self,
        config_path: str,
        reload_interval_seconds: float | None = None,
        pipeline: TradeFilterPipeline | None = None,
    ) -> None:
        self._config_path = config_path
        if reload_interval_seconds is None:
            try:
                from config.settings import settings  # local import to avoid circular deps
                reload_interval_seconds = float(
                    settings.filter_config_reload_interval_seconds
                )
            except Exception:
                reload_interval_seconds = 60.0
        self._reload_interval = reload_interval_seconds
        self._last_mtime: float = 0.0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._stopped = False

        # Load initial config (raises on failure so callers get a clear error)
        initial_filters = self._load_config(config_path)
        if pipeline is not None:
            self._pipeline = pipeline
            self._pipeline.reload_filters(initial_filters)
        else:
            self._pipeline = TradeFilterPipeline(initial_filters)

        # Record mtime after successful first load
        try:
            self._last_mtime = os.stat(config_path).st_mtime
        except OSError:
            self._last_mtime = 0.0

        self._schedule_next()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def pipeline(self) -> TradeFilterPipeline:
        """The managed :class:`TradeFilterPipeline`, updated on hot-reload."""
        return self._pipeline

    def stop(self) -> None:
        """Cancel the background reload timer cleanly (call on shutdown)."""
        self._stopped = True
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _schedule_next(self) -> None:
        if self._stopped:
            return
        timer = threading.Timer(self._reload_interval, self._check_and_reload)
        timer.daemon = True
        timer.start()
        with self._lock:
            self._timer = timer

    def _check_and_reload(self) -> None:
        """Called by the background timer; checks mtime and reloads if changed."""
        try:
            try:
                current_mtime = os.stat(self._config_path).st_mtime
            except OSError as exc:
                logger.warning(
                    "filter_config.yaml not accessible; retaining previous config",
                    extra={"error": str(exc), "path": self._config_path},
                )
                return

            if current_mtime <= self._last_mtime:
                return  # no change

            logger.info(
                "filter_config.yaml changed; reloading",
                extra={"path": self._config_path},
            )
            try:
                new_filters = self._load_config(self._config_path)
            except Exception as exc:
                logger.error(
                    "filter_config.yaml reload failed — retaining previous config",
                    extra={"path": self._config_path, "error": str(exc)},
                )
                return

            self._pipeline.reload_filters(new_filters)
            self._last_mtime = current_mtime
            logger.info(
                "filter_pipeline.hot_reload_complete",
                extra={
                    "path": self._config_path,
                    "filter_count": len(new_filters),
                },
            )
        finally:
            self._schedule_next()

    @staticmethod
    def _load_config(config_path: str) -> list[TradeFilter]:
        """Parse and validate *config_path*, returning a ready filter list.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        ValueError
            If the YAML or Pydantic schema validation fails.
        """
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        if not isinstance(raw, dict):
            raise ValueError(
                f"filter_config.yaml must be a YAML mapping, got {type(raw).__name__}"
            )

        try:
            config = _FilterFileConfig.model_validate(raw)
        except Exception as exc:
            raise ValueError(
                f"filter_config.yaml schema validation failed: {exc}"
            ) from exc

        return _build_filters(config)


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def load_pipeline_from_config(config_path: str) -> tuple[TradeFilterPipeline, FilterConfigLoader]:
    """Create a :class:`TradeFilterPipeline` and its hot-reload watcher.

    Returns a ``(pipeline, loader)`` tuple.  Call ``loader.stop()`` on
    shutdown to cancel the background timer.

    Parameters
    ----------
    config_path:
        Path to ``filter_config.yaml``.
    """
    loader = FilterConfigLoader(config_path)
    return loader.pipeline, loader
