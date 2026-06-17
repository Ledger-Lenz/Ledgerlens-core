"""Throughput benchmarks for the async pipeline.

Run with:
    pytest -m benchmark tests/test_throughput.py -v

These tests are excluded from standard CI because they measure wall-clock
performance rather than correctness.  The benchmark methodology uses 100 ms
simulated Horizon latency per request: a correct concurrent implementation
finishes 500 accounts in ~2.5 s (500/20 batches × 100 ms); a purely
sequential one would take 50 s and fail the 10 s threshold.
"""

import asyncio
import time

import joblib
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestClassifier

import run_pipeline
from detection.feature_engineering import FEATURE_NAMES
from ingestion.account_loader import async_load_account_metadata
from ingestion.http_client import AsyncHorizonClient
from ingestion.synthetic_data import generate_synthetic_dataset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_client(latency: float = 0.10, max_concurrency: int = 20) -> AsyncHorizonClient:
    """Return an AsyncHorizonClient whose HTTP layer sleeps `latency` seconds per call."""
    client = AsyncHorizonClient("https://horizon.stellar.org", max_concurrency=max_concurrency)

    async def _slow_get(url, params=None):
        await asyncio.sleep(latency)
        # Return a minimal valid Horizon-shaped payload so parse helpers don't crash.
        class _Resp:
            status_code = 200
            request = None

            def json(self):
                return {"_embedded": {"records": []}, "_links": {}}

            def raise_for_status(self):
                pass

        return _Resp()

    client._client.get = _slow_get  # type: ignore[assignment]
    return client


def _save_temp_models(tmp_path):
    X = [[0] * len(FEATURE_NAMES), [1] * len(FEATURE_NAMES)]
    y = [0, 1]
    model = RandomForestClassifier(n_estimators=3, random_state=0).fit(X, y)
    models_path = tmp_path / "models"
    models_path.mkdir()
    for name in ("random_forest", "xgboost", "lightgbm"):
        joblib.dump(model, models_path / f"{name}.joblib")
    return str(models_path)


# ---------------------------------------------------------------------------
# Benchmark: async_load_account_metadata concurrency
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_throughput_500_wallets():
    """500 accounts with 100 ms simulated latency must complete in < 10 s.

    Correct concurrent code: ~2.5 s (500 / 20 concurrent × 100 ms).
    Sequential code would take 50 s — the 10 s threshold proves concurrency.
    """
    accounts = [f"G{'A' * 4}{i:051d}"[:56] for i in range(500)]
    client = _make_mock_client(latency=0.10, max_concurrency=20)

    start = time.monotonic()
    result = await async_load_account_metadata(accounts, client)
    elapsed = time.monotonic() - start

    await client.close()

    assert len(result) == 500
    assert elapsed < 10.0, (
        f"async_load_account_metadata took {elapsed:.2f}s for 500 accounts at 100ms latency; "
        "expected < 10s — possible regression to sequential fetching"
    )


# ---------------------------------------------------------------------------
# Benchmark: async_run end-to-end with mocked network
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_async_run_throughput_500_wallets(tmp_path, monkeypatch):
    """async_run I/O layer must handle 500 wallets in < 10 s with 100 ms simulated latency.

    `build_feature_vector` is mocked to be instant so the benchmark isolates the
    async I/O concurrency proof: 500 accounts / 20 concurrent × 100 ms ≈ 2.5 s.
    A sequential implementation would take 50 s and fail the 10 s threshold.
    """
    import config.settings as settings_module

    from detection.feature_engineering import FEATURE_NAMES as _FN

    # Build a synthetic dataset with ~500 accounts
    trades, account_metadata, _events, _ = generate_synthetic_dataset(
        n_normal_accounts=450, n_wash_rings=10, ring_size=5, seed=42
    )

    models_path = _save_temp_models(tmp_path)
    object.__setattr__(settings_module.settings, "model_dir", models_path)
    object.__setattr__(settings_module.settings, "db_path", str(tmp_path / "ledgerlens.db"))
    object.__setattr__(settings_module.settings, "score_contract_id", "")
    object.__setattr__(settings_module.settings, "service_secret_key", "")

    # Mock historical trades (no real HTTP pagination needed).
    async def _return_trades(**kwargs):
        return trades

    monkeypatch.setattr(run_pipeline, "async_load_historical_trades", _return_trades)

    # Mock build_feature_vector to be instant — the benchmark tests I/O throughput,
    # not feature engineering speed (which is CPU-bound and measured separately).
    monkeypatch.setattr(
        run_pipeline,
        "build_feature_vector",
        lambda *args, **kwargs: dict.fromkeys(_FN, 0.0),
    )

    # Replace the AsyncHorizonClient with a mock that sleeps 100 ms per request.
    mock_client = _make_mock_client(latency=0.10, max_concurrency=20)

    class _MockClientCM:
        async def __aenter__(self):
            return mock_client

        async def __aexit__(self, *_):
            await mock_client.close()

    monkeypatch.setattr(run_pipeline, "AsyncHorizonClient", lambda *a, **kw: _MockClientCM())

    start = time.monotonic()
    scores = await run_pipeline.async_run(
        asset_pairs=[(None, "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")]
    )
    elapsed = time.monotonic() - start

    n_accounts = len(pd.unique(trades[["base_account", "counter_account"]].values.ravel()))
    assert len(scores) == n_accounts
    assert elapsed < 10.0, (
        f"async_run I/O layer took {elapsed:.2f}s for {n_accounts} accounts at 100ms latency; "
        "expected < 10s — possible regression to sequential fetching"
    )


# ---------------------------------------------------------------------------
# Unit: async_load_account_metadata returns correct data for 100 accounts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_load_account_metadata_100_accounts():
    """Returns the right mapping shape and neutral values for accounts with no history."""
    accounts = [f"G{'B' * 4}{i:051d}"[:56] for i in range(100)]
    client = _make_mock_client(latency=0.0, max_concurrency=20)

    result = await async_load_account_metadata(accounts, client)

    assert set(result.keys()) == set(accounts)
    for info in result.values():
        assert info["funding_source"] is None
        assert info["created_at"] is None

    await client.close()
