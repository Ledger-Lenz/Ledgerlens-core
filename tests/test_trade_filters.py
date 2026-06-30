"""Tests for ingestion/filters.py — trade filter pipeline.

Coverage areas
--------------
Unit:
  - FilterResult
  - AssetPairWhitelistFilter (pass, reject, empty whitelist)
  - AssetPairBlacklistFilter (pass, reject, empty blacklist)
  - MinimumVolumeFilter (pass, reject, zero threshold, bad field)
  - AssetTypeFilter (pass, reject, mixed types)
  - AccountExclusionFilter (pass, reject, invalid Stellar key at init)
  - TradeFilterPipeline (short-circuit, all pass, stats, reset_stats)
  - FilterConfigLoader (valid YAML, invalid YAML retains previous, hot-reload)
  - _is_valid_stellar_public_key helper

Integration:
  - 100-trade batch through pipeline; 50 whitelisted, 50 not
  - Rejected trades persisted to filtered_trades SQLite table
  - Hot-reload: new config activates within reload interval
  - store_filtered_trade + prune_filtered_trades

Edge cases:
  - Native XLM trade (no issuer) against asset pair whitelist
  - Empty pipeline (no filters) — all pass
  - Concurrent apply + reload
  - Trade with paging_token=None falls back to id

Performance:
  - 10 000 trades through 5-filter pipeline in < 100 ms
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ingestion.data_models import Asset, Trade, TradeType
from ingestion.filters import (
    AccountExclusionFilter,
    AssetPairBlacklistFilter,
    AssetPairWhitelistFilter,
    AssetTypeFilter,
    FilterConfigLoader,
    FilterResult,
    MinimumVolumeFilter,
    TradeFilter,
    TradeFilterPipeline,
    _is_valid_stellar_public_key,
    load_pipeline_from_config,
)

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

_KNOWN_ACCOUNT = "GBIX3P3SOAACVWEQ6J6DE47I7SY4RYNU4UX7IGSSBCTA3NR4LKLTYCEG"
_ACCOUNT_B     = "GBHL3FUJ3IUTE4W5PLCIAL2X7RBIS4ST2NQ44TNFM74YDCJ3YNALZFUZ"
_INVALID_KEY   = "NOTAVALIDKEY"

NATIVE_ASSET = Asset(code="XLM", issuer=None)
USDC_ASSET   = Asset(
    code="USDC",
    issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
)
BTC_ASSET = Asset(
    code="BTC",
    issuer="GAUTUYY2THLF7SGITDFMXJVYH3LHDSMGEAKSBU267M2K7A3W543CKUEF",
)
# 12-character credit asset (credit_alphanum12)
LONGCODE_ASSET = Asset(
    code="STELLARTOKEN",
    issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
)


def _make_trade(
    *,
    base_asset: Asset = NATIVE_ASSET,
    counter_asset: Asset = USDC_ASSET,
    base_amount: float = 10.0,
    counter_amount: float = 5.0,
    price: float = 0.5,
    base_account: str = _KNOWN_ACCOUNT,
    counter_account: str | None = _ACCOUNT_B,
    trade_id: str = "trade-001",
    paging_token: str | None = "1234567890",
) -> Trade:
    return Trade(
        id=trade_id,
        paging_token=paging_token,
        ledger_close_time=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        base_account=base_account,
        counter_account=counter_account,
        base_asset=base_asset,
        counter_asset=counter_asset,
        base_amount=base_amount,
        counter_amount=counter_amount,
        price=price,
        base_is_seller=True,
        trade_type=TradeType.ORDERBOOK,
    )


# ---------------------------------------------------------------------------
# Helper: write a minimal filter_config.yaml to a temp file
# ---------------------------------------------------------------------------

def _write_config(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


def _minimal_config(filters: list[dict] | None = None) -> dict:
    return {"version": "1.0", "filters": filters or []}


# ============================================================
# FilterResult
# ============================================================

class TestFilterResult:
    def test_passed_repr(self):
        r = FilterResult(passed=True)
        assert "passed=True" in repr(r)

    def test_failed_repr(self):
        r = FilterResult(passed=False, reason="too small")
        assert "passed=False" in repr(r)
        assert "too small" in repr(r)

    def test_passed_no_reason(self):
        r = FilterResult(passed=True)
        assert r.reason is None

    def test_failed_has_reason(self):
        r = FilterResult(passed=False, reason="x")
        assert r.reason == "x"


# ============================================================
# _is_valid_stellar_public_key
# ============================================================

class TestStellarKeyValidator:
    def test_known_valid_key(self):
        assert _is_valid_stellar_public_key(_KNOWN_ACCOUNT) is True

    def test_known_valid_key_b(self):
        assert _is_valid_stellar_public_key(_ACCOUNT_B) is True

    def test_too_short(self):
        assert _is_valid_stellar_public_key("G" + "A" * 54) is False

    def test_too_long(self):
        assert _is_valid_stellar_public_key("G" + "A" * 56) is False

    def test_wrong_prefix(self):
        assert _is_valid_stellar_public_key("S" + "A" * 55) is False

    def test_invalid_characters(self):
        assert _is_valid_stellar_public_key("G" + "!" * 55) is False

    def test_empty_string(self):
        assert _is_valid_stellar_public_key("") is False

    def test_non_string(self):
        assert _is_valid_stellar_public_key(12345) is False  # type: ignore[arg-type]


# ============================================================
# AssetPairWhitelistFilter
# ============================================================

class TestAssetPairWhitelistFilter:
    def test_passes_when_pair_in_whitelist(self):
        f = AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"})
        result = f.apply(_make_trade())
        assert result.passed is True

    def test_rejects_when_pair_not_in_whitelist(self):
        f = AssetPairWhitelistFilter(allowed_pairs={"XLM/BTC"})
        result = f.apply(_make_trade())
        assert result.passed is False
        assert "XLM/USDC" in result.reason
        assert "whitelist" in result.reason

    def test_empty_whitelist_allows_all(self):
        """Empty whitelist = pass-through; do not reject any pair."""
        f = AssetPairWhitelistFilter(allowed_pairs=set())
        result = f.apply(_make_trade())
        assert result.passed is True

    def test_rejection_count_increments(self):
        f = AssetPairWhitelistFilter(allowed_pairs={"XLM/BTC"})
        f.apply(_make_trade())
        f.apply(_make_trade())
        assert f.rejection_count == 2

    def test_passing_trade_does_not_increment_count(self):
        f = AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"})
        f.apply(_make_trade())
        assert f.rejection_count == 0

    def test_native_xlm_pair_symbol(self):
        """XLM (native) asset code is just 'XLM', not 'XLM:None'."""
        f = AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"})
        trade = _make_trade(base_asset=NATIVE_ASSET)
        assert f.apply(trade).passed is True

    def test_reset_stats(self):
        f = AssetPairWhitelistFilter(allowed_pairs={"XLM/BTC"})
        f.apply(_make_trade())
        f.reset_stats()
        assert f.rejection_count == 0


# ============================================================
# AssetPairBlacklistFilter
# ============================================================

class TestAssetPairBlacklistFilter:
    def test_passes_when_pair_not_blacklisted(self):
        f = AssetPairBlacklistFilter(blocked_pairs={"TEST/XLM"})
        result = f.apply(_make_trade())
        assert result.passed is True

    def test_rejects_when_pair_blacklisted(self):
        f = AssetPairBlacklistFilter(blocked_pairs={"XLM/USDC"})
        result = f.apply(_make_trade())
        assert result.passed is False
        assert "XLM/USDC" in result.reason
        assert "blacklisted" in result.reason

    def test_empty_blacklist_allows_all(self):
        f = AssetPairBlacklistFilter(blocked_pairs=set())
        result = f.apply(_make_trade())
        assert result.passed is True

    def test_rejection_count(self):
        f = AssetPairBlacklistFilter(blocked_pairs={"XLM/USDC"})
        f.apply(_make_trade())
        f.apply(_make_trade())
        assert f.rejection_count == 2

    def test_reset_stats(self):
        f = AssetPairBlacklistFilter(blocked_pairs={"XLM/USDC"})
        f.apply(_make_trade())
        f.reset_stats()
        assert f.rejection_count == 0


# ============================================================
# MinimumVolumeFilter
# ============================================================

class TestMinimumVolumeFilter:
    def test_passes_above_threshold(self):
        f = MinimumVolumeFilter(min_volume=Decimal("1.0"))
        result = f.apply(_make_trade(base_amount=10.0))
        assert result.passed is True

    def test_passes_at_threshold(self):
        f = MinimumVolumeFilter(min_volume=Decimal("10.0"))
        result = f.apply(_make_trade(base_amount=10.0))
        assert result.passed is True

    def test_rejects_below_threshold(self):
        f = MinimumVolumeFilter(min_volume=Decimal("100.0"))
        result = f.apply(_make_trade(base_amount=10.0))
        assert result.passed is False
        assert "below minimum" in result.reason

    def test_zero_threshold_allows_all(self):
        f = MinimumVolumeFilter(min_volume=Decimal("0"))
        result = f.apply(_make_trade(base_amount=0.000001))
        assert result.passed is True

    def test_counter_amount_field(self):
        f = MinimumVolumeFilter(min_volume=Decimal("10.0"), volume_field="counter_amount")
        result = f.apply(_make_trade(counter_amount=5.0))
        assert result.passed is False

    def test_price_field(self):
        f = MinimumVolumeFilter(min_volume=Decimal("1.0"), volume_field="price")
        result = f.apply(_make_trade(price=0.5))
        assert result.passed is False

    def test_invalid_volume_field_raises(self):
        with pytest.raises(ValueError, match="volume_field must be one of"):
            MinimumVolumeFilter(min_volume=Decimal("1.0"), volume_field="bad_field")

    def test_negative_min_volume_raises(self):
        with pytest.raises(ValueError, match="min_volume must be >= 0"):
            MinimumVolumeFilter(min_volume=Decimal("-1.0"))

    def test_rejection_count(self):
        f = MinimumVolumeFilter(min_volume=Decimal("100.0"))
        f.apply(_make_trade(base_amount=1.0))
        f.apply(_make_trade(base_amount=2.0))
        assert f.rejection_count == 2

    def test_reset_stats(self):
        f = MinimumVolumeFilter(min_volume=Decimal("100.0"))
        f.apply(_make_trade(base_amount=1.0))
        f.reset_stats()
        assert f.rejection_count == 0


# ============================================================
# AssetTypeFilter
# ============================================================

class TestAssetTypeFilter:
    def test_passes_native_when_native_allowed(self):
        f = AssetTypeFilter(allowed_types={"native", "credit_alphanum4"})
        result = f.apply(_make_trade(base_asset=NATIVE_ASSET, counter_asset=USDC_ASSET))
        assert result.passed is True

    def test_passes_alphanum4_when_allowed(self):
        f = AssetTypeFilter(allowed_types={"native", "credit_alphanum4"})
        result = f.apply(_make_trade(base_asset=NATIVE_ASSET, counter_asset=USDC_ASSET))
        assert result.passed is True

    def test_rejects_alphanum12_when_not_allowed(self):
        f = AssetTypeFilter(allowed_types={"native", "credit_alphanum4"})
        result = f.apply(_make_trade(counter_asset=LONGCODE_ASSET))
        assert result.passed is False
        assert "credit_alphanum12" in result.reason

    def test_rejects_native_when_not_allowed(self):
        f = AssetTypeFilter(allowed_types={"credit_alphanum4"})
        result = f.apply(_make_trade(base_asset=NATIVE_ASSET))
        assert result.passed is False
        assert "native" in result.reason

    def test_all_types_allowed(self):
        f = AssetTypeFilter(
            allowed_types={"native", "credit_alphanum4", "credit_alphanum12"}
        )
        result = f.apply(_make_trade(counter_asset=LONGCODE_ASSET))
        assert result.passed is True

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="Invalid asset types"):
            AssetTypeFilter(allowed_types={"native", "credit_alphanum999"})  # type: ignore[arg-type]

    def test_rejection_count(self):
        f = AssetTypeFilter(allowed_types={"native"})
        f.apply(_make_trade(counter_asset=USDC_ASSET))   # USDC = credit_alphanum4 → rejected
        assert f.rejection_count == 1

    def test_reset_stats(self):
        f = AssetTypeFilter(allowed_types={"native"})
        f.apply(_make_trade(counter_asset=USDC_ASSET))
        f.reset_stats()
        assert f.rejection_count == 0


# ============================================================
# AccountExclusionFilter
# ============================================================

class TestAccountExclusionFilter:
    def test_passes_when_accounts_not_excluded(self):
        f = AccountExclusionFilter(excluded_accounts={_KNOWN_ACCOUNT})
        trade = _make_trade(base_account=_ACCOUNT_B, counter_account=None)
        assert f.apply(trade).passed is True

    def test_rejects_excluded_base_account(self):
        f = AccountExclusionFilter(excluded_accounts={_KNOWN_ACCOUNT})
        trade = _make_trade(base_account=_KNOWN_ACCOUNT)
        result = f.apply(trade)
        assert result.passed is False
        assert "base_account" in result.reason
        assert "exclusion list" in result.reason

    def test_rejects_excluded_counter_account(self):
        f = AccountExclusionFilter(excluded_accounts={_ACCOUNT_B})
        trade = _make_trade(base_account=_KNOWN_ACCOUNT, counter_account=_ACCOUNT_B)
        result = f.apply(trade)
        assert result.passed is False
        assert "counter_account" in result.reason

    def test_empty_exclusion_set_allows_all(self):
        f = AccountExclusionFilter(excluded_accounts=set())
        trade = _make_trade(base_account=_KNOWN_ACCOUNT)
        assert f.apply(trade).passed is True

    def test_none_counter_account_is_safe(self):
        """Liquidity-pool trades have no counter_account; must not raise."""
        f = AccountExclusionFilter(excluded_accounts={_ACCOUNT_B})
        trade = _make_trade(
            base_account=_KNOWN_ACCOUNT,
            counter_account=None,
        )
        assert f.apply(trade).passed is True

    def test_invalid_stellar_key_raises_on_init(self):
        with pytest.raises(ValueError, match="Invalid Stellar public keys"):
            AccountExclusionFilter(excluded_accounts={_INVALID_KEY})

    def test_partially_valid_keys_raises(self):
        with pytest.raises(ValueError, match="Invalid Stellar public keys"):
            AccountExclusionFilter(
                excluded_accounts={_KNOWN_ACCOUNT, _INVALID_KEY}
            )

    def test_rejection_count(self):
        f = AccountExclusionFilter(excluded_accounts={_KNOWN_ACCOUNT})
        f.apply(_make_trade(base_account=_KNOWN_ACCOUNT))
        f.apply(_make_trade(base_account=_KNOWN_ACCOUNT))
        assert f.rejection_count == 2

    def test_reset_stats(self):
        f = AccountExclusionFilter(excluded_accounts={_KNOWN_ACCOUNT})
        f.apply(_make_trade(base_account=_KNOWN_ACCOUNT))
        f.reset_stats()
        assert f.rejection_count == 0


# ============================================================
# TradeFilterPipeline
# ============================================================

class TestTradeFilterPipeline:
    def test_empty_pipeline_passes_all(self):
        pipeline = TradeFilterPipeline(filters=[])
        assert pipeline.apply(_make_trade()).passed is True

    def test_all_filters_pass(self):
        pipeline = TradeFilterPipeline(filters=[
            AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"}),
            MinimumVolumeFilter(min_volume=Decimal("1.0")),
        ])
        assert pipeline.apply(_make_trade(base_amount=5.0)).passed is True

    def test_first_filter_rejects_short_circuits(self):
        """When the first filter rejects, subsequent filters are not called."""
        reject_filter = AssetPairWhitelistFilter(allowed_pairs={"XLM/BTC"})
        second_filter = MinimumVolumeFilter(min_volume=Decimal("0"))

        pipeline = TradeFilterPipeline(filters=[reject_filter, second_filter])
        result = pipeline.apply(_make_trade())

        assert result.passed is False
        assert "asset_pair_whitelist" in result.reason
        # Second filter never ran, so its rejection count stays 0
        assert second_filter.rejection_count == 0

    def test_second_filter_rejects(self):
        first_filter = AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"})
        second_filter = MinimumVolumeFilter(min_volume=Decimal("1000.0"))

        pipeline = TradeFilterPipeline(filters=[first_filter, second_filter])
        result = pipeline.apply(_make_trade(base_amount=5.0))

        assert result.passed is False
        assert "minimum_volume" in result.reason
        assert first_filter.rejection_count == 0
        assert second_filter.rejection_count == 1

    def test_reason_is_prefixed_with_filter_name(self):
        pipeline = TradeFilterPipeline(filters=[
            AssetPairBlacklistFilter(blocked_pairs={"XLM/USDC"}),
        ])
        result = pipeline.apply(_make_trade())
        assert result.reason.startswith("asset_pair_blacklist:")

    def test_stats_returns_dict_of_rejection_counts(self):
        f1 = AssetPairWhitelistFilter(allowed_pairs={"XLM/BTC"})
        f2 = MinimumVolumeFilter(min_volume=Decimal("0"))
        pipeline = TradeFilterPipeline(filters=[f1, f2])
        pipeline.apply(_make_trade())  # rejected by f1
        stats = pipeline.stats()
        assert stats == {"asset_pair_whitelist": 1, "minimum_volume": 0}

    def test_reset_stats_zeroes_all_filters(self):
        f = AssetPairBlacklistFilter(blocked_pairs={"XLM/USDC"})
        pipeline = TradeFilterPipeline(filters=[f])
        pipeline.apply(_make_trade())
        pipeline.reset_stats()
        assert pipeline.stats() == {"asset_pair_blacklist": 0}

    def test_reload_filters_atomically_swaps(self):
        old_filter = AssetPairWhitelistFilter(allowed_pairs={"XLM/BTC"})
        pipeline = TradeFilterPipeline(filters=[old_filter])

        # Trade rejected by old filter (XLM/USDC not in {"XLM/BTC"})
        assert pipeline.apply(_make_trade()).passed is False

        # Swap to a permissive filter
        pipeline.reload_filters([AssetPairWhitelistFilter(allowed_pairs=set())])

        # Now passes
        assert pipeline.apply(_make_trade()).passed is True

    def test_reload_with_empty_list_allows_all(self):
        pipeline = TradeFilterPipeline(filters=[
            AssetPairBlacklistFilter(blocked_pairs={"XLM/USDC"}),
        ])
        pipeline.reload_filters([])
        assert pipeline.apply(_make_trade()).passed is True


# ============================================================
# FilterConfigLoader — unit tests
# ============================================================

class TestFilterConfigLoader:
    """Tests for YAML loading, schema validation, and hot-reload."""

    def test_loads_valid_config(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), _minimal_config([
            {"type": "asset_pair_whitelist", "enabled": True, "pairs": ["XLM/USDC"]},
        ]))
        loader = FilterConfigLoader(str(config_file), reload_interval_seconds=9999)
        try:
            result = loader.pipeline.apply(_make_trade())
            assert result.passed is True  # XLM/USDC is whitelisted
        finally:
            loader.stop()

    def test_rejects_trade_from_loaded_config(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), _minimal_config([
            {"type": "asset_pair_blacklist", "enabled": True, "pairs": ["XLM/USDC"]},
        ]))
        loader = FilterConfigLoader(str(config_file), reload_interval_seconds=9999)
        try:
            result = loader.pipeline.apply(_make_trade())
            assert result.passed is False
        finally:
            loader.stop()

    def test_disabled_filter_is_skipped(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), _minimal_config([
            {"type": "asset_pair_blacklist", "enabled": False, "pairs": ["XLM/USDC"]},
        ]))
        loader = FilterConfigLoader(str(config_file), reload_interval_seconds=9999)
        try:
            result = loader.pipeline.apply(_make_trade())
            assert result.passed is True
        finally:
            loader.stop()

    def test_invalid_yaml_raises_on_initial_load(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        config_file.write_text("{ not valid yaml: [}", encoding="utf-8")
        with pytest.raises(Exception):
            FilterConfigLoader(str(config_file), reload_interval_seconds=9999)

    def test_invalid_schema_raises_on_initial_load(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), {"version": "1.0", "filters": [
            {"type": "unknown_filter_type", "enabled": True},
        ]})
        with pytest.raises(Exception):
            FilterConfigLoader(str(config_file), reload_interval_seconds=9999)

    def test_invalid_reload_retains_previous_config(self, tmp_path):
        """If a hot-reload produces an invalid config, the previous filters remain."""
        config_file = tmp_path / "filter_config.yaml"
        # Initial valid config: no filters (pass-through)
        _write_config(str(config_file), _minimal_config())
        loader = FilterConfigLoader(str(config_file), reload_interval_seconds=9999)
        pipeline = loader.pipeline
        try:
            assert pipeline.apply(_make_trade()).passed is True

            # Write invalid YAML to the file
            config_file.write_text("!!! invalid yaml: [broken", encoding="utf-8")
            # Force mtime to change so the loader sees it
            os.utime(str(config_file), (time.time() + 1, time.time() + 1))

            # Manually trigger the reload check (simulating timer firing)
            loader._last_mtime = 0.0  # force mtime mismatch
            loader._check_and_reload()

            # Previous config (pass-through) should still be in place
            assert pipeline.apply(_make_trade()).passed is True
        finally:
            loader.stop()

    def test_invalid_stellar_key_in_config_raises(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), _minimal_config([
            {
                "type": "account_exclusion",
                "enabled": True,
                "excluded_accounts": ["NOTAVALIDKEY"],
            }
        ]))
        with pytest.raises(Exception):
            FilterConfigLoader(str(config_file), reload_interval_seconds=9999)

    def test_hot_reload_activates_new_filter(self, tmp_path):
        """After the file changes, reloading picks up the new blacklist."""
        config_file = tmp_path / "filter_config.yaml"
        # Start with no filters
        _write_config(str(config_file), _minimal_config())
        loader = FilterConfigLoader(str(config_file), reload_interval_seconds=9999)
        pipeline = loader.pipeline
        try:
            assert pipeline.apply(_make_trade()).passed is True

            # Update config to blacklist XLM/USDC
            _write_config(str(config_file), _minimal_config([
                {"type": "asset_pair_blacklist", "enabled": True, "pairs": ["XLM/USDC"]},
            ]))
            # Force the loader to see the change
            loader._last_mtime = 0.0
            loader._check_and_reload()

            assert pipeline.apply(_make_trade()).passed is False
        finally:
            loader.stop()

    def test_stop_cancels_timer(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), _minimal_config())
        loader = FilterConfigLoader(str(config_file), reload_interval_seconds=9999)
        loader.stop()
        assert loader._timer is None or not loader._timer.is_alive()

    def test_load_pipeline_from_config_factory(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), _minimal_config())
        pipeline, loader = load_pipeline_from_config(str(config_file))
        try:
            assert isinstance(pipeline, TradeFilterPipeline)
            assert isinstance(loader, FilterConfigLoader)
        finally:
            loader.stop()

    def test_unsupported_version_raises(self, tmp_path):
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), {"version": "99.0", "filters": []})
        with pytest.raises(Exception):
            FilterConfigLoader(str(config_file), reload_interval_seconds=9999)

    def test_all_filter_types_load_correctly(self, tmp_path):
        """Smoke-test that all 5 filter types parse without error."""
        config_file = tmp_path / "filter_config.yaml"
        _write_config(str(config_file), _minimal_config([
            {"type": "asset_pair_whitelist", "enabled": True, "pairs": ["XLM/USDC"]},
            {"type": "asset_pair_blacklist", "enabled": True, "pairs": ["TEST/XLM"]},
            {
                "type": "minimum_volume",
                "enabled": True,
                "min_volume": "0.01",
                "volume_field": "base_amount",
            },
            {
                "type": "asset_type",
                "enabled": True,
                "allowed_types": ["native", "credit_alphanum4"],
            },
            {
                "type": "account_exclusion",
                "enabled": True,
                "excluded_accounts": [_KNOWN_ACCOUNT],
            },
        ]))
        loader = FilterConfigLoader(str(config_file), reload_interval_seconds=9999)
        try:
            assert loader.pipeline is not None
        finally:
            loader.stop()


# ============================================================
# Integration: SQLite storage
# ============================================================

class TestStorageIntegration:
    """Integration tests for store_filtered_trade and prune_filtered_trades."""

    def _make_db(self, tmp_path) -> str:
        db_path = str(tmp_path / "test.db")
        from detection.storage import init_db
        init_db(db_path)
        return db_path

    def test_store_filtered_trade_persists_row(self, tmp_path):
        from detection.storage import store_filtered_trade
        db_path = self._make_db(tmp_path)
        trade = _make_trade(trade_id="t1", paging_token="pt1")
        store_filtered_trade(trade, "test: unit test rejection", db_path=db_path)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT id, paging_token, rejection_reason FROM filtered_trades").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "t1"
        assert rows[0][1] == "pt1"
        assert rows[0][2] == "test: unit test rejection"

    def test_store_filtered_trade_uses_id_when_paging_token_none(self, tmp_path):
        from detection.storage import store_filtered_trade
        db_path = self._make_db(tmp_path)
        trade = _make_trade(trade_id="trade-no-paging", paging_token=None)
        store_filtered_trade(trade, "reason", db_path=db_path)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT paging_token FROM filtered_trades").fetchall()
        conn.close()
        assert rows[0][0] == "trade-no-paging"

    def test_store_filtered_trade_duplicate_ignored(self, tmp_path):
        from detection.storage import store_filtered_trade
        db_path = self._make_db(tmp_path)
        trade = _make_trade(trade_id="t1", paging_token="pt1")
        store_filtered_trade(trade, "reason", db_path=db_path)
        store_filtered_trade(trade, "reason", db_path=db_path)  # duplicate

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM filtered_trades").fetchone()[0]
        conn.close()
        assert count == 1

    def test_prune_filtered_trades_removes_oldest(self, tmp_path):
        from detection.storage import prune_filtered_trades, store_filtered_trade
        db_path = self._make_db(tmp_path)

        # Insert 10 trades
        for i in range(10):
            trade = _make_trade(trade_id=f"t{i}", paging_token=f"pt{i}")
            store_filtered_trade(trade, "reason", db_path=db_path)

        # Prune with max_rows=5 → should keep 4 (90% of 5 = 4.5 → 4)
        deleted = prune_filtered_trades(max_rows=5, db_path=db_path)
        conn = sqlite3.connect(db_path)
        remaining = conn.execute("SELECT COUNT(*) FROM filtered_trades").fetchone()[0]
        conn.close()
        assert deleted > 0
        assert remaining <= 5

    def test_prune_no_op_when_below_limit(self, tmp_path):
        from detection.storage import prune_filtered_trades, store_filtered_trade
        db_path = self._make_db(tmp_path)

        for i in range(3):
            trade = _make_trade(trade_id=f"t{i}", paging_token=f"pt{i}")
            store_filtered_trade(trade, "reason", db_path=db_path)

        deleted = prune_filtered_trades(max_rows=100, db_path=db_path)
        assert deleted == 0


# ============================================================
# Integration: 100-trade batch through pipeline
# ============================================================

class TestPipelineBatchIntegration:
    """Run 100 trades through a whitelist pipeline; assert 50 pass, 50 rejected."""

    def _make_batch(self) -> list[Trade]:
        trades = []
        for i in range(50):
            # Whitelisted pair: XLM/USDC
            trades.append(_make_trade(
                base_asset=NATIVE_ASSET,
                counter_asset=USDC_ASSET,
                trade_id=f"pass-{i}",
                paging_token=f"pt-pass-{i}",
            ))
        for i in range(50):
            # Not whitelisted: XLM/BTC
            trades.append(_make_trade(
                base_asset=NATIVE_ASSET,
                counter_asset=BTC_ASSET,
                trade_id=f"fail-{i}",
                paging_token=f"pt-fail-{i}",
            ))
        return trades

    def test_fifty_pass_fifty_rejected(self):
        pipeline = TradeFilterPipeline(filters=[
            AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"}),
        ])
        trades = self._make_batch()
        passed = sum(1 for t in trades if pipeline.apply(t).passed)
        rejected = sum(1 for t in trades if not pipeline.apply(t).passed)
        assert passed == 50
        assert rejected == 50

    def test_rejected_trades_stored_in_sqlite(self, tmp_path):
        from detection.storage import init_db, store_filtered_trade
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        pipeline = TradeFilterPipeline(filters=[
            AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"}),
        ])
        trades = self._make_batch()
        for trade in trades:
            result = pipeline.apply(trade)
            if not result.passed:
                store_filtered_trade(trade, result.reason, db_path=db_path)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM filtered_trades").fetchone()[0]
        conn.close()
        assert count == 50


# ============================================================
# Edge cases
# ============================================================

class TestEdgeCases:
    def test_native_xlm_against_whitelist_with_code_format(self):
        """A native XLM trade uses code 'XLM'; whitelist should match it."""
        pipeline = TradeFilterPipeline(filters=[
            AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"}),
        ])
        trade = _make_trade(base_asset=NATIVE_ASSET, counter_asset=USDC_ASSET)
        assert pipeline.apply(trade).passed is True

    def test_empty_pipeline_all_pass(self):
        pipeline = TradeFilterPipeline(filters=[])
        for _ in range(20):
            assert pipeline.apply(_make_trade()).passed is True

    def test_trade_with_no_paging_token(self):
        """Trades without paging_token (paging_token=None) do not raise."""
        pipeline = TradeFilterPipeline(filters=[
            MinimumVolumeFilter(min_volume=Decimal("0.001")),
        ])
        trade = _make_trade(paging_token=None, base_amount=5.0)
        assert pipeline.apply(trade).passed is True

    def test_liquidity_pool_trade_no_counter_account(self):
        """Pool trades have counter_account=None; AccountExclusionFilter must handle it."""
        f = AccountExclusionFilter(excluded_accounts={_ACCOUNT_B})
        trade = Trade(
            id="pool-trade-1",
            paging_token="pt-pool-1",
            ledger_close_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
            base_account=_KNOWN_ACCOUNT,
            counter_account=None,
            base_asset=NATIVE_ASSET,
            counter_asset=USDC_ASSET,
            base_amount=100.0,
            counter_amount=50.0,
            price=0.5,
            base_is_seller=True,
            trade_type=TradeType.LIQUIDITY_POOL,
            liquidity_pool_id="pool-001",
        )
        assert f.apply(trade).passed is True

    def test_concurrent_apply_and_reload(self):
        """Concurrent apply() and reload_filters() must not raise."""
        pipeline = TradeFilterPipeline(filters=[
            AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC"}),
        ])
        errors = []

        def applier():
            for _ in range(200):
                try:
                    pipeline.apply(_make_trade())
                except Exception as exc:
                    errors.append(exc)

        def reloader():
            for i in range(10):
                try:
                    pipeline.reload_filters([
                        AssetPairWhitelistFilter(
                            allowed_pairs={"XLM/USDC" if i % 2 == 0 else "XLM/BTC"}
                        ),
                    ])
                    time.sleep(0.001)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=applier) for _ in range(4)]
        threads.append(threading.Thread(target=reloader))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"Concurrent access raised: {errors}"

    def test_minimum_volume_filter_with_very_small_decimal(self):
        """Verify Decimal precision is preserved for tiny thresholds."""
        f = MinimumVolumeFilter(min_volume=Decimal("0.000001"))
        trade = _make_trade(base_amount=0.0000001)
        assert f.apply(trade).passed is False

    def test_pipeline_stats_names_match_filter_names(self):
        f1 = AssetPairWhitelistFilter(allowed_pairs=set())
        f2 = AccountExclusionFilter(excluded_accounts=set())
        pipeline = TradeFilterPipeline(filters=[f1, f2])
        stats = pipeline.stats()
        assert "asset_pair_whitelist" in stats
        assert "account_exclusion" in stats


# ============================================================
# Performance benchmark
# ============================================================

class TestPerformance:
    """10 000 trades through a 5-filter pipeline must complete in < 100 ms."""

    def _build_pipeline(self) -> TradeFilterPipeline:
        return TradeFilterPipeline(filters=[
            AssetPairWhitelistFilter(allowed_pairs={"XLM/USDC", "XLM/BTC"}),
            AssetPairBlacklistFilter(blocked_pairs={"TEST/XLM"}),
            MinimumVolumeFilter(min_volume=Decimal("0.001")),
            AssetTypeFilter(
                allowed_types={"native", "credit_alphanum4", "credit_alphanum12"}
            ),
            AccountExclusionFilter(excluded_accounts=set()),
        ])

    def _build_trades(self, n: int) -> list[Trade]:
        pairs = [
            (NATIVE_ASSET, USDC_ASSET),
            (NATIVE_ASSET, BTC_ASSET),
            (USDC_ASSET, BTC_ASSET),
        ]
        trades = []
        for i in range(n):
            base, counter = pairs[i % len(pairs)]
            trades.append(_make_trade(
                base_asset=base,
                counter_asset=counter,
                base_amount=float(i % 100 + 1),
                trade_id=f"perf-{i}",
                paging_token=f"pt-perf-{i}",
            ))
        return trades

    def test_10k_trades_under_100ms(self):
        pipeline = self._build_pipeline()
        trades = self._build_trades(10_000)

        start = time.perf_counter()
        for trade in trades:
            pipeline.apply(trade)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 100, (
            f"10 000 trades took {elapsed_ms:.1f} ms — expected < 100 ms"
        )
