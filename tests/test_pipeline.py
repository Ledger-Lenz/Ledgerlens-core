from collections.abc import Iterator
from datetime import datetime
from unittest.mock import MagicMock, patch

import joblib
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestClassifier

import run_pipeline
from detection.feature_engineering import FEATURE_NAMES
from detection.storage import get_latest_scores
from ingestion.data_models import Asset, Trade
from ingestion.synthetic_data import generate_synthetic_dataset


# Ensure stellar_sdk is mockable for Soroban integration tests.
# This must be done before detection.soroban_publisher is first imported.
import sys as _sys

_sys.modules.setdefault("stellar_sdk", MagicMock())
_sys.modules.setdefault("stellar_sdk.operation", MagicMock())
# Pre-import so patching detection.soroban_publisher.SorobanPublisher works
from detection import soroban_publisher as _soroban_publisher  # noqa: E402, F401


def _make_trade(
    base_account: str,
    counter_account: str | None,
    idx: int = 0,
    ts: datetime | None = None,
) -> Trade:
    return Trade(
        id=f"trade-{idx}",
        ledger_close_time=ts or datetime(2024, 1, 1, 12, 0, 0),
        base_account=base_account,
        counter_account=counter_account,
        base_asset=Asset(code="XLM"),
        counter_asset=Asset(
            code="USDC",
            issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
        ),
        base_amount=100.0,
        counter_amount=200.0,
        price=2.0,
        base_is_seller=True,
    )


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    X = [[0] * len(FEATURE_NAMES), [1] * len(FEATURE_NAMES)]
    y = [0, 1]
    model = RandomForestClassifier(n_estimators=5, random_state=0).fit(X, y)

    model_path = tmp_path / "models"
    model_path.mkdir()
    for name in ("random_forest", "xgboost", "lightgbm"):
        joblib.dump(model, model_path / f"{name}.joblib")

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "model_dir", str(model_path))
    object.__setattr__(settings_module.settings, "db_path", str(tmp_path / "ledgerlens.db"))
    return str(model_path)


def test_run_persists_scores(model_dir, monkeypatch):
    trades, account_metadata, events, _labels = generate_synthetic_dataset(
        n_normal_accounts=3, n_wash_rings=1, ring_size=2, trades_per_normal=4, trades_per_wash=4
    )

    calls = []

    def fake_load_order_book_events_for_pair(base_asset, counter_asset, since):
        calls.append((base_asset, counter_asset, since))
        return []

    monkeypatch.setattr(run_pipeline, "load_historical_trades", lambda **kwargs: trades)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: account_metadata)
    monkeypatch.setattr(run_pipeline, "load_order_book_events_for_pair", fake_load_order_book_events_for_pair)
    monkeypatch.setattr(run_pipeline, "load_path_payments_for_accounts", lambda accounts, since: [])

    scores = run_pipeline.run(asset_pairs=[(None, "USDC:ISSUER")])

    accounts = pd.unique(trades[["base_account", "counter_account"]].values.ravel())
    assert len(calls) == 1
    assert calls[0][:2] == (None, "USDC:ISSUER")
    assert len(scores) == len(accounts)
    for s in scores:
        assert 0 <= s.score <= 100

    import config.settings as settings_module

    stored = get_latest_scores(db_path=settings_module.settings.db_path)
    assert len(stored) == len(scores)


def test_submit_batch_called_for_high_risk_scores(model_dir, monkeypatch):
    """Soroban submit_batch is called when contract_id and secret_key are set."""
    trades, account_metadata, events, _labels = generate_synthetic_dataset(
        n_normal_accounts=3, n_wash_rings=1, ring_size=2, trades_per_normal=4, trades_per_wash=4
    )

    monkeypatch.setattr(run_pipeline, "load_historical_trades", lambda **kwargs: trades)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: account_metadata)
    monkeypatch.setattr(run_pipeline, "load_order_book_events_for_pair", lambda base, counter, since: [])
    monkeypatch.setattr(run_pipeline, "load_path_payments_for_accounts", lambda accounts, since: [])

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "score_contract_id", "CA3CQ7C6YHK6K6C6J6C6K6C6K6C6K6C6K6")
    object.__setattr__(settings_module.settings, "service_secret_key", "SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    object.__setattr__(settings_module.settings, "risk_score_threshold", 70)

    mock_publisher = MagicMock()
    mock_publisher.submit_batch.return_value = {"G0000:XLM/USDC": "mock_tx_hash"}

    with patch("detection.soroban_publisher.SorobanPublisher", return_value=mock_publisher):
        run_pipeline.run(asset_pairs=[(None, "USDC:ISSUER")])

    mock_publisher.submit_batch.assert_called_once()
    passed_scores = mock_publisher.submit_batch.call_args[0][0]
    assert all(s.score >= 70 for s in passed_scores)


def test_no_submit_flag_skips_on_chain(model_dir, monkeypatch):
    """Passing no_submit=True skips on-chain submission."""
    trades, account_metadata, events, _labels = generate_synthetic_dataset(
        n_normal_accounts=3, n_wash_rings=1, ring_size=2, trades_per_normal=4, trades_per_wash=4
    )

    monkeypatch.setattr(run_pipeline, "load_historical_trades", lambda **kwargs: trades)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: account_metadata)
    monkeypatch.setattr(run_pipeline, "load_order_book_events_for_pair", lambda base, counter, since: [])
    monkeypatch.setattr(run_pipeline, "load_path_payments_for_accounts", lambda accounts, since: [])

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "score_contract_id", "CA3CQ7C6YHK6K6C6J6C6K6C6K6C6K6C6K6")
    object.__setattr__(settings_module.settings, "service_secret_key", "SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    mock_publisher = MagicMock()
    with patch("detection.soroban_publisher.SorobanPublisher", return_value=mock_publisher):
        run_pipeline.run(asset_pairs=[(None, "USDC:ISSUER")], no_submit=True)

    mock_publisher.submit_batch.assert_not_called()


def test_submit_skipped_when_not_configured(model_dir, monkeypatch):
    """Submission is skipped when contract_id or secret_key is empty."""
    import config.settings as settings_module

    trades, account_metadata, events, _labels = generate_synthetic_dataset(
        n_normal_accounts=3, n_wash_rings=1, ring_size=2, trades_per_normal=4, trades_per_wash=4
    )

    monkeypatch.setattr(run_pipeline, "load_historical_trades", lambda **kwargs: trades)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: account_metadata)
    monkeypatch.setattr(run_pipeline, "load_order_book_events_for_pair", lambda base, counter, since: [])
    monkeypatch.setattr(run_pipeline, "load_path_payments_for_accounts", lambda accounts, since: [])

    # Ensure Soroban settings are empty (reset from any earlier test)
    object.__setattr__(settings_module.settings, "score_contract_id", "")
    object.__setattr__(settings_module.settings, "service_secret_key", "")

    mock_publisher = MagicMock()
    with patch("detection.soroban_publisher.SorobanPublisher", return_value=mock_publisher):
        run_pipeline.run(asset_pairs=[(None, "USDC:ISSUER")])

    mock_publisher.submit_batch.assert_not_called()


def test_run_records_scored_features(model_dir, monkeypatch):
    """Scored features should be recorded for drift detection."""
    trades, account_metadata, events, _labels = generate_synthetic_dataset(
        n_normal_accounts=3, n_wash_rings=1, ring_size=2, trades_per_normal=4, trades_per_wash=4
    )

    monkeypatch.setattr(run_pipeline, "load_historical_trades", lambda **kwargs: trades)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: account_metadata)
    monkeypatch.setattr(run_pipeline, "load_order_book_events_for_pair", lambda base, counter, since: [])
    monkeypatch.setattr(run_pipeline, "load_path_payments_for_accounts", lambda accounts, since: [])

    with patch("run_pipeline.record_scored_features") as mock_record:
        run_pipeline.run(asset_pairs=[(None, "USDC:ISSUER")])

        # Verify record_scored_features was called
        mock_record.assert_called_once()

        # Verify it was called with feature vectors and wallet IDs
        args, _kwargs = mock_record.call_args
        feature_vectors, wallet_ids, asset_pairs = args

        assert len(feature_vectors) > 0
        assert len(wallet_ids) == len(feature_vectors)
        assert len(asset_pairs) == len(feature_vectors)
        assert all(isinstance(fv, dict) for fv in feature_vectors)


def test_async_run_records_scored_features(model_dir, monkeypatch):
    """Async run should also record scored features for drift detection."""
    import asyncio

    trades, account_metadata, events, _labels = generate_synthetic_dataset(
        n_normal_accounts=3, n_wash_rings=1, ring_size=2, trades_per_normal=4, trades_per_wash=4
    )

    async def fake_async_load(*args, **kwargs):
        return trades

    async def fake_async_metadata(*args, **kwargs):
        return account_metadata

    async def fake_async_order_book(*args, **kwargs):
        return []

    async def fake_async_path_payments(*args, **kwargs):
        return []

    monkeypatch.setattr(run_pipeline, "async_load_historical_trades", fake_async_load)
    monkeypatch.setattr(run_pipeline, "async_load_account_metadata", fake_async_metadata)
    monkeypatch.setattr(run_pipeline, "async_load_order_book_events_for_pair", fake_async_order_book)
    monkeypatch.setattr(run_pipeline, "async_load_path_payments", fake_async_path_payments)

    with patch("run_pipeline.record_scored_features") as mock_record:
        asyncio.run(run_pipeline.async_run(asset_pairs=[(None, "USDC:ISSUER")]))

        # Verify record_scored_features was called
        mock_record.assert_called_once()

        # Verify it was called with feature vectors and wallet IDs
        args, _kwargs = mock_record.call_args
        feature_vectors, wallet_ids, asset_pairs = args

        assert len(feature_vectors) > 0
        assert len(wallet_ids) == len(feature_vectors)
        assert len(asset_pairs) == len(feature_vectors)


# ---------------------------------------------------------------------------
# Streaming pipeline tests
# ---------------------------------------------------------------------------


def test_run_streaming_flushes_on_batch_size(model_dir, monkeypatch, tmp_path):
    """Flush triggers after batch_size trades accumulate."""
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "cursor_path", str(tmp_path / "cursor.txt"))

    trade_list = [_make_trade(f"G{i}", f"G{i+1}", i) for i in range(12)]

    def mock_stream(cursor: str = "now") -> Iterator[tuple[Trade, str]]:
        for t in trade_list:
            yield t, f"cursor-{t.id}"

    monkeypatch.setattr(run_pipeline, "stream_trades_with_cursor", mock_stream)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: {})

    flush_args: list[int] = []

    def mock_flush(buffer, models, pair_key, asset_pair, cursor):
        flush_args.append(len(buffer))

    monkeypatch.setattr(run_pipeline, "_flush_streaming_buffer", mock_flush)

    run_pipeline.run_streaming(
        batch_size=5, flush_interval_seconds=999, _now=lambda: 0.0
    )

    # 12 trades / batch_size=5 → two full flushes of 5 each
    # (remaining 2 trades stay in buffer since stream_trades is infinite IRL)
    assert flush_args == [5, 5], f"Expected [5, 5] but got {flush_args}"


def test_run_streaming_flushes_on_time_interval(model_dir, monkeypatch, tmp_path):
    """Flush triggers when flush_interval_seconds elapses."""
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "cursor_path", str(tmp_path / "cursor.txt"))

    trade_list = [_make_trade(f"G{i}", f"G{i+1}", i) for i in range(3)]

    def mock_stream(cursor: str = "now") -> Iterator[tuple[Trade, str]]:
        for t in trade_list:
            yield t, f"cursor-{t.id}"

    monkeypatch.setattr(run_pipeline, "stream_trades_with_cursor", mock_stream)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: {})

    # _now is called 4 times: 1× for last_flush_time init + 1× per trade
    # last_flush_time=0, trade0 @0, trade1 @0, trade2 @31 → triggers flush
    time_values = iter([0.0, 0.0, 0.0, 31.0])
    monkeypatch.setattr(run_pipeline, "_flush_streaming_buffer", lambda *a: None)

    flush_calls: list[float] = []

    def track_flush(buffer, models, pair_key, asset_pair, cursor):
        flush_calls.append(len(buffer))

    monkeypatch.setattr(run_pipeline, "_flush_streaming_buffer", track_flush)

    run_pipeline.run_streaming(
        batch_size=999, flush_interval_seconds=30, _now=lambda: next(time_values)
    )

    # Only 3 trades, batch_size=999 so no batch trigger; the 3rd trade's _now
    # call returns 31 which exceeds flush_interval_seconds=30 → 1 flush.
    assert len(flush_calls) == 1, f"Expected 1 flush, got {flush_calls}"
    assert flush_calls[0] == 3


def test_run_streaming_scores_persisted(model_dir, monkeypatch, tmp_path):
    """After a flush, scores are persisted to the store."""
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "cursor_path", str(tmp_path / "cursor.txt"))

    trade_list = [
        _make_trade("GAAA", "GBBB", 0),
        _make_trade("GCCC", "GDDD", 1),
    ]

    def mock_stream(cursor: str = "now") -> Iterator[tuple[Trade, str]]:
        for t in trade_list:
            yield t, f"cursor-{t.id}"

    monkeypatch.setattr(run_pipeline, "stream_trades_with_cursor", mock_stream)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: {})

    # Build a features dict with all required keys
    mock_features = {name: 0.0 for name in FEATURE_NAMES}

    monkeypatch.setattr(
        run_pipeline, "build_feature_vector", lambda *a, **kw: mock_features
    )
    monkeypatch.setattr(
        run_pipeline,
        "score_feature_vector",
        lambda models, features: (0.5, 0.8),
    )
    monkeypatch.setattr(run_pipeline, "record_scored_features", lambda *a, **kw: None)

    with patch("run_pipeline.save_scores") as mock_save:
        run_pipeline.run_streaming(
            batch_size=2, flush_interval_seconds=999, _now=lambda: 0.0
        )

    mock_save.assert_called_once()
    saved = mock_save.call_args[0][0]
    assert len(saved) == 4  # GAAA, GBBB, GCCC, GDDD
    for s in saved:
        assert 0 <= s.score <= 100


def test_run_streaming_cursor_persisted(model_dir, monkeypatch, tmp_path):
    """Cursor file is written after each flush."""
    cursor_file = tmp_path / "cursor.txt"

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "cursor_path", str(cursor_file))

    trade_list = [_make_trade(f"G{i}", f"G{i+1}", i) for i in range(5)]

    def mock_stream(cursor: str = "now") -> Iterator[tuple[Trade, str]]:
        for i, t in enumerate(trade_list):
            yield t, f"paging-token-{i}"

    monkeypatch.setattr(run_pipeline, "stream_trades_with_cursor", mock_stream)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: {})

    mock_features = {name: 0.0 for name in FEATURE_NAMES}
    monkeypatch.setattr(
        run_pipeline, "build_feature_vector", lambda *a, **kw: mock_features
    )
    monkeypatch.setattr(
        run_pipeline,
        "score_feature_vector",
        lambda models, features: (0.5, 0.8),
    )
    monkeypatch.setattr(run_pipeline, "record_scored_features", lambda *a, **kw: None)
    monkeypatch.setattr(run_pipeline, "save_scores", lambda *a, **kw: None)

    run_pipeline.run_streaming(
        batch_size=5, flush_interval_seconds=999, _now=lambda: 0.0
    )

    assert cursor_file.exists()
    content = cursor_file.read_text().strip()
    assert content == "paging-token-4"  # last trade's cursor


def test_run_streaming_keyboard_interrupt_flushes(model_dir, monkeypatch, tmp_path):
    """KeyboardInterrupt mid-stream triggers a final flush."""
    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "cursor_path", str(tmp_path / "cursor.txt"))

    trade_list = [_make_trade(f"G{i}", f"G{i+1}", i) for i in range(3)]

    def mock_stream(cursor: str = "now") -> Iterator[tuple[Trade, str]]:
        for t in trade_list:
            yield t, f"cursor-{t.id}"

    monkeypatch.setattr(run_pipeline, "stream_trades_with_cursor", mock_stream)
    monkeypatch.setattr(run_pipeline, "load_account_metadata", lambda accounts: {})

    flush_calls: list[int] = []

    def mock_flush(buffer, models, pair_key, asset_pair, cursor):
        flush_calls.append(len(buffer))

    monkeypatch.setattr(run_pipeline, "_flush_streaming_buffer", mock_flush)

    # Raise KeyboardInterrupt after 2 trades (on the 3rd iteration)
    # We simulate this by patching _flush_streaming_buffer to raise on the first real flush.
    # Actually, let's make stream_trades_with_cursor raise after yielding 2 trades.

    original_stream = mock_stream

    def interrupting_stream(cursor="now"):
        gen = original_stream(cursor)
        yield next(gen)  # trade 0
        yield next(gen)  # trade 1
        raise KeyboardInterrupt()

    monkeypatch.setattr(run_pipeline, "stream_trades_with_cursor", interrupting_stream)

    with pytest.raises(KeyboardInterrupt):
        run_pipeline.run_streaming(
            batch_size=999, flush_interval_seconds=999, _now=lambda: 0.0
        )

    # Should have flushed the 2 buffered trades on interrupt
    assert len(flush_calls) >= 1
    assert flush_calls[-1] == 2
