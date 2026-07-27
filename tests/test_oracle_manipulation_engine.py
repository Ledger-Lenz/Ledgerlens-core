"""Tests for the flash-loan price-manipulation detector."""

import pytest
import pandas as pd

from detection.oracle_manipulation_engine import (
    FlashLoanManipulationCandidate,
    build_pool_depth_map,
    candidate_to_alert,
    candidates_to_alerts,
    compute_flash_loan_manipulation_score,
    compute_volatility_band,
    detect_flash_loan_manipulation,
)
from detection.storage import get_flash_loan_alerts, save_alerts
from ingestion.data_models import Asset, LiquidityPool, TradeType

XLM = {"code": "XLM", "issuer": None}
USDC = {"code": "USDC", "issuer": "GISSUER"}
BASE_TS = pd.Timestamp("2026-06-01T00:00:00Z")


def _trade(
    *,
    account,
    is_seller,
    price,
    amount,
    ledger,
    op=0,
    pool_id="P1",
    seconds=None,
):
    return {
        "id": f"{account}-{ledger}-{op}",
        "ledger_close_time": BASE_TS + pd.Timedelta(seconds=seconds if seconds is not None else ledger * 10),
        "base_account": account,
        "counter_account": None,
        "base_asset": XLM,
        "counter_asset": USDC,
        "base_amount": amount,
        "counter_amount": amount * price,
        "price": price,
        "base_is_seller": is_seller,
        "trade_type": TradeType.LIQUIDITY_POOL,
        "liquidity_pool_id": pool_id,
        "ledger_sequence": ledger,
        "operation_order": op,
    }


def _stable_window(pool_id="P1"):
    """Build 50 trades at a stable price (~1.00) so the trailing band is tight,
    followed by a flash-loan manipulation pattern (large buy then reversal)."""
    rows = []
    for i in range(50):
        rows.append(
            _trade(
                account="STABLE",
                is_seller=(i % 2 == 0),
                price=1.00 + (i % 3 - 1) * 0.001,
                amount=100,
                ledger=1 + i // 10,
                op=i % 10,
                pool_id=pool_id,
                seconds=i * 10,
            )
        )
    # The attacker appears at ledger 10 with no prior history
    # Large buy at an inflated price (manipulation)
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=False,
            price=1.50,  # far outside the stable band
            amount=5000,
            ledger=10,
            op=0,
            pool_id=pool_id,
            seconds=500,
        )
    )
    # Same-block reversal: attacker sells to return to near-zero position
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=True,
            price=1.48,
            amount=5000,
            ledger=10,
            op=1,
            pool_id=pool_id,
            seconds=500,
        )
    )
    return pd.DataFrame(rows).astype({"trade_type": object})


# ---------------------------------------------------------------------------
# Unit: volatility band
# ---------------------------------------------------------------------------


def test_compute_volatility_band_insufficient_data():
    mean, std = compute_volatility_band(pd.DataFrame({"price": [1.0, 1.0]}), 1)
    assert pd.isna(mean)
    assert pd.isna(std)


def test_compute_volatility_band_normal():
    df = pd.DataFrame({"price": [1.0, 1.01, 1.02, 1.01, 1.0, 1.01, 1.02, 1.01]})
    mean, std = compute_volatility_band(df, 7, rolling_window=10)
    assert mean is not None and not pd.isna(mean)
    assert std is not None and not pd.isna(std)
    assert std > 0


# ---------------------------------------------------------------------------
# Flash-loan pattern detection
# ---------------------------------------------------------------------------


def test_detects_flash_loan_manipulation():
    df = _stable_window()
    # Provide pool depth so the share requirement passes
    depth_map = {"P1": 50000.0}
    candidates = detect_flash_loan_manipulation(
        df,
        pool_id_to_depth=depth_map,
        price_deviation_sigma=3.0,
        min_pool_share_pct=0.05,  # 5000/50000 = 10% > 5%
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert isinstance(c, FlashLoanManipulationCandidate)
    assert c.account == "ATTACKER"
    assert c.pool_id == "P1"
    assert c.price_deviation_sigma > 3.0
    assert c.reversal_trade_id is not None
    assert c.reversed_within_blocks == 0  # same block
    assert c.prior_position_ratio < 0.1
    assert c.confidence >= 0.5


def test_reversal_over_window_does_not_flag():
    """Manipulation reversed outside the block window should NOT be flagged."""
    rows = []
    for i in range(50):
        rows.append(
            _trade(
                account="STABLE",
                is_seller=(i % 2 == 0),
                price=1.00 + (i % 3 - 1) * 0.001,
                amount=100,
                ledger=1 + i // 10,
                op=i % 10,
                seconds=i * 10,
            )
        )
    # Manipulation at ledger 10
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=False,
            price=1.50,
            amount=5000,
            ledger=10,
            op=0,
            seconds=500,
        )
    )
    # Reversal at ledger 12 (2 blocks later, but default window is 1)
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=True,
            price=1.48,
            amount=5000,
            ledger=12,
            op=0,
            seconds=520,
        )
    )
    df = pd.DataFrame(rows).astype({"trade_type": object})
    candidates = detect_flash_loan_manipulation(
        df,
        pool_id_to_depth={"P1": 50000.0},
        price_deviation_sigma=3.0,
        reversal_window_blocks=1,
    )
    assert len(candidates) == 0


def test_legitimate_large_trade_not_flagged():
    """A trader with meaningful prior exposure should NOT be flagged."""
    rows = []
    # Build up a clear buy-side position first (organic exposure)
    for i in range(10):
        rows.append(
            _trade(
                account="ATTACKER",
                is_seller=False,  # always buying = building position
                price=1.00 + (i % 3 - 1) * 0.001,
                amount=1000,
                ledger=1 + i // 10,
                op=i % 10,
                seconds=i * 10,
            )
        )
    # Add stable background trades
    for i in range(40):
        rows.append(
            _trade(
                account="STABLE",
                is_seller=(i % 2 == 0),
                price=1.00 + (i % 3 - 1) * 0.001,
                amount=100,
                ledger=6 + i // 10,
                op=i % 10,
                seconds=60 + i * 10,
            )
        )
    # Now a large buy at an elevated price (attempted manipulation)
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=False,
            price=1.50,
            amount=5000,
            ledger=10,
            op=5,
            seconds=450,
        )
    )
    # Reversal
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=True,
            price=1.48,
            amount=5000,
            ledger=10,
            op=6,
            seconds=450,
        )
    )
    df = pd.DataFrame(rows).astype({"trade_type": object})
    candidates = detect_flash_loan_manipulation(
        df,
        pool_id_to_depth={"P1": 50000.0},
        price_deviation_sigma=3.0,
    )
    # ATTACKER has prior_position_ratio from earlier buys (~10000/5000 = 2.0), should not be flagged
    assert len(candidates) == 0


def test_empty_data_returns_empty():
    assert detect_flash_loan_manipulation(pd.DataFrame()) == []


def test_no_pool_trades_returns_empty():
    """Trades without LIQUIDITY_POOL trade_type should not be processed."""
    df = pd.DataFrame([{"trade_type": TradeType.ORDERBOOK, "liquidity_pool_id": None}]).astype({"trade_type": object})
    assert detect_flash_loan_manipulation(df) == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_partial_reversal_below_tolerance():
    """A partial reversal below the tolerance threshold should NOT flag."""
    rows = []
    for i in range(50):
        rows.append(
            _trade(
                account="STABLE",
                is_seller=(i % 2 == 0),
                price=1.00 + (i % 3 - 1) * 0.001,
                amount=100,
                ledger=1 + i // 10,
                op=i % 10,
                seconds=i * 10,
            )
        )
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=False,
            price=1.50,
            amount=5000,
            ledger=10,
            op=0,
            seconds=500,
        )
    )
    # Only sell back 100 (2% of 5000), well below the tolerance
    rows.append(
        _trade(
            account="ATTACKER",
            is_seller=True,
            price=1.48,
            amount=100,
            ledger=10,
            op=1,
            seconds=500,
        )
    )
    df = pd.DataFrame(rows).astype({"trade_type": object})
    candidates = detect_flash_loan_manipulation(
        df,
        pool_id_to_depth={"P1": 50000.0},
        price_deviation_sigma=3.0,
    )
    assert len(candidates) == 0


def test_thin_pool_no_depth_map_skips_share_check():
    """When pool_id_to_depth is None, the depth-share requirement is skipped."""
    df = _stable_window()
    candidates = detect_flash_loan_manipulation(
        df,
        pool_id_to_depth=None,
        price_deviation_sigma=3.0,
    )
    # Should still detect because depth check is skipped
    assert len(candidates) >= 1


# ---------------------------------------------------------------------------
# Alert conversion
# ---------------------------------------------------------------------------


def test_candidate_to_alert():
    c = FlashLoanManipulationCandidate(
        account="A1",
        pool_id="P1",
        manipulating_trade_id="50",
        price_before=1.0,
        price_at_peak=1.5,
        price_deviation_sigma=8.0,
        reversal_trade_id="51",
        reversed_within_blocks=0,
        prior_position_ratio=0.0,
        confidence=0.95,
    )
    alert = candidate_to_alert(c, asset_pair="XLM/USDC")
    assert alert["alert_type"] == "FLASH_LOAN_MANIPULATION"
    assert alert["wallet"] == "A1"
    assert alert["pool_id"] == "P1"
    assert alert["asset_pair"] == "XLM/USDC"
    assert alert["detail"]["price_deviation_sigma"] == 8.0
    assert alert["detail"]["confidence"] == 0.95


def test_candidates_to_alerts_empty():
    assert candidates_to_alerts([]) == []


# ---------------------------------------------------------------------------
# compute_flash_loan_manipulation_score
# ---------------------------------------------------------------------------


def test_compute_score_returns_zero_for_innocent():
    df = _stable_window()
    score = compute_flash_loan_manipulation_score(df, "STABLE", pool_id_to_depth={"P1": 50000.0})
    assert score == 0.0


def test_compute_score_returns_positive_for_attacker():
    df = _stable_window()
    score = compute_flash_loan_manipulation_score(
        df, "ATTACKER", pool_id_to_depth={"P1": 50000.0}, price_deviation_sigma=3.0
    )
    assert score > 0.0


def test_compute_score_empty_data():
    assert compute_flash_loan_manipulation_score(pd.DataFrame(), "ANY") == 0.0


# ---------------------------------------------------------------------------
# build_pool_depth_map
# ---------------------------------------------------------------------------


def test_build_pool_depth_map():
    asset = Asset(code="XLM", issuer=None)
    pool = LiquidityPool(
        id="P1",
        fee_bp=30,
        total_shares=100000.0,
        reserves=[(asset, 50000.0), (asset, 50000.0)],
    )
    depth_map = build_pool_depth_map({"P1": pool})
    assert depth_map["P1"] == 100000.0


def test_build_pool_depth_map_empty():
    assert build_pool_depth_map(None) == {}
    assert build_pool_depth_map({}) == {}


# ---------------------------------------------------------------------------
# Storage: get_flash_loan_alerts
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "oracle_manipulation.db")


def test_get_flash_loan_alerts(db_path):
    """Verify that flash-loan alerts can be saved and retrieved."""
    alert = {
        "alert_type": "FLASH_LOAN_MANIPULATION",
        "wallet": "GA_TEST",
        "asset_pair": "XLM/USDC",
        "pool_id": "P1",
        "detail": {
            "manipulating_trade_id": "50",
            "price_before": 1.0,
            "price_at_peak": 1.5,
            "price_deviation_sigma": 8.0,
            "reversal_trade_id": "51",
            "reversed_within_blocks": 0,
            "prior_position_ratio": 0.0,
            "confidence": 0.95,
        },
    }
    save_alerts([alert], db_path=db_path)
    alerts = get_flash_loan_alerts(db_path=db_path)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "FLASH_LOAN_MANIPULATION"
    assert alerts[0]["wallet"] == "GA_TEST"
    assert alerts[0]["detail"]["price_deviation_sigma"] == 8.0


def test_get_flash_loan_alerts_filters_wallet(db_path):
    alerts = [
        {
            "alert_type": "FLASH_LOAN_MANIPULATION",
            "wallet": "GA_A",
            "asset_pair": "XLM/USDC",
            "pool_id": "P1",
            "detail": {},
        },
        {
            "alert_type": "FLASH_LOAN_MANIPULATION",
            "wallet": "GA_B",
            "asset_pair": "XLM/USDC",
            "pool_id": "P1",
            "detail": {},
        },
    ]
    save_alerts(alerts, db_path=db_path)
    result = get_flash_loan_alerts(wallet="GA_A", db_path=db_path)
    assert len(result) == 1
    assert result[0]["wallet"] == "GA_A"


def test_get_flash_loan_alerts_only_returns_flash_loan_type(db_path):
    """It should only return FLASH_LOAN_MANIPULATION alerts, not other types."""
    save_alerts(
        [
            {"alert_type": "FLASH_LOAN_MANIPULATION", "wallet": "GA_A", "asset_pair": "XLM/USDC", "pool_id": "P1", "detail": {}},
            {"alert_type": "SANDWICH_ATTACK", "wallet": "GA_B", "asset_pair": "XLM/USDC", "pool_id": "P1", "detail": {}},
        ],
        db_path=db_path,
    )
    result = get_flash_loan_alerts(db_path=db_path)
    assert len(result) == 1
    assert result[0]["wallet"] == "GA_A"
