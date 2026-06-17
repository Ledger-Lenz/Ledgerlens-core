from unittest.mock import MagicMock, patch

import joblib
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestClassifier

import run_pipeline
from detection.feature_engineering import FEATURE_NAMES
from detection.storage import get_latest_scores
from ingestion.synthetic_data import generate_synthetic_dataset


# Ensure stellar_sdk is mockable for Soroban integration tests.
# This must be done before detection.soroban_publisher is first imported.
import sys as _sys

_sys.modules.setdefault("stellar_sdk", MagicMock())
_sys.modules.setdefault("stellar_sdk.operation", MagicMock())
# Pre-import so patching detection.soroban_publisher.SorobanPublisher works
from detection import soroban_publisher as _soroban_publisher  # noqa: E402, F401


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

    monkeypatch.setattr(run_pipeline, "async_load_historical_trades", fake_async_load)
    monkeypatch.setattr(run_pipeline, "async_load_account_metadata", fake_async_metadata)
    monkeypatch.setattr(run_pipeline, "async_load_order_book_events_for_pair", fake_async_order_book)

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
