"""Tests for detection/benford_baseline.py — Benford baseline calibration."""

import json
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from detection.benford_baseline import BenfordBaseline, BenfordBaselineCalibrator


class TestBenfordBaselineCalibrator:
    """Tests for BenfordBaselineCalibrator.calibrate and .load."""

    def test_calibrate_from_trades_table(self, tmp_path):
        """Calibrate computes digit frequencies from the trades table and
        persists a BenfordBaseline row."""
        db = str(tmp_path / "test.db")

        # Create trades and benford_baselines tables
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS trades (
                    paging_token TEXT PRIMARY KEY,
                    base_asset_code TEXT,
                    base_asset_issuer TEXT,
                    counter_asset_code TEXT,
                    counter_asset_issuer TEXT,
                    base_amount REAL,
                    ledger_close_time TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS benford_baselines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_pair TEXT NOT NULL UNIQUE,
                    digit_freqs_json TEXT NOT NULL,
                    trade_count INTEGER NOT NULL,
                    computed_at TEXT NOT NULL,
                    window_days INTEGER NOT NULL
                )"""
            )
            now = datetime.now(timezone.utc)
            # Insert trades for a single asset pair — amounts with known first digits
            amounts = [123.0, 456.0, 123.0, 789.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
            for i, amt in enumerate(amounts):
                conn.execute(
                    """INSERT OR IGNORE INTO trades
                       (paging_token, base_asset_code, counter_asset_code,
                        base_amount, ledger_close_time)
                       VALUES (?, ?, ?, ?, ?)""",
                    (f"tok_{i}", "XLM", "USDC", amt, now.isoformat()),
                )
            conn.commit()

        calibrator = BenfordBaselineCalibrator(db_path=db)
        baseline = calibrator.calibrate("XLM/USDC", window_days=30)

        assert isinstance(baseline, BenfordBaseline)
        assert baseline.asset_pair == "XLM/USDC"
        assert len(baseline.digit_freqs) == 9
        assert math.isclose(sum(baseline.digit_freqs), 1.0, rel_tol=1e-9)
        assert baseline.trade_count == len(amounts)
        assert baseline.window_days == 30
        assert baseline.computed_at is not None

        # First digits: 123→1, 456→4, 123→1, 789→7, 1→1, 2→2, 3→3, 4→4,
        # 5→5, 6→6, 7→7, 8→8, 9→9. Digit 1 appears 3 times (123.0×2 + 1.0).
        assert baseline.digit_freqs[0] == pytest.approx(3 / 13)

    def test_load_persisted_baseline(self, tmp_path):
        """load returns the previously persisted baseline."""
        db = str(tmp_path / "test.db")

        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS benford_baselines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_pair TEXT NOT NULL,
                    digit_freqs_json TEXT NOT NULL,
                    trade_count INTEGER NOT NULL,
                    computed_at TEXT NOT NULL,
                    window_days INTEGER NOT NULL
                )"""
            )
            freqs = [0.3, 0.2, 0.1, 0.1, 0.05, 0.05, 0.05, 0.05, 0.1]
            now = datetime.now(timezone.utc)
            conn.execute(
                """INSERT INTO benford_baselines
                   (asset_pair, digit_freqs_json, trade_count, computed_at, window_days)
                   VALUES (?, ?, ?, ?, ?)""",
                ("XLM/USDC", json.dumps(freqs), 100, now.isoformat(), 30),
            )
            conn.commit()

        calibrator = BenfordBaselineCalibrator(db_path=db)
        baseline = calibrator.load("XLM/USDC")

        assert baseline is not None
        assert baseline.asset_pair == "XLM/USDC"
        assert baseline.digit_freqs == freqs
        assert baseline.trade_count == 100
        assert baseline.window_days == 30

    def test_load_missing_baseline(self, tmp_path):
        """load returns None when no baseline exists for the asset pair."""
        db = str(tmp_path / "test.db")

        calibrator = BenfordBaselineCalibrator(db_path=db)
        baseline = calibrator.load("NONEXISTENT/PAIR")
        assert baseline is None

    def test_calibrate_falls_back_to_theoretical_benford(self, tmp_path):
        """When the trades table has no matching rows, calibrate falls back to
        the theoretical Benford distribution."""
        db = str(tmp_path / "test.db")

        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS trades (
                    paging_token TEXT PRIMARY KEY,
                    base_asset_code TEXT,
                    counter_asset_code TEXT,
                    base_amount REAL,
                    ledger_close_time TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS benford_baselines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_pair TEXT NOT NULL UNIQUE,
                    digit_freqs_json TEXT NOT NULL,
                    trade_count INTEGER NOT NULL,
                    computed_at TEXT NOT NULL,
                    window_days INTEGER NOT NULL
                )"""
            )
            conn.commit()

        calibrator = BenfordBaselineCalibrator(db_path=db)
        baseline = calibrator.calibrate("XLM/BTC", window_days=30)

        assert baseline.trade_count == 0
        assert len(baseline.digit_freqs) == 9
        # Should match theoretical Benford: log10(1+1/d)
        for d in range(1, 10):
            expected = math.log10(1 + 1 / d)
            assert math.isclose(baseline.digit_freqs[d - 1], expected, rel_tol=1e-9)

    def test_calibrate_persists_and_overwrites(self, tmp_path):
        """Calling calibrate twice updates the stored baseline via ON CONFLICT."""
        db = str(tmp_path / "test.db")

        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS benford_baselines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_pair TEXT NOT NULL UNIQUE,
                    digit_freqs_json TEXT NOT NULL,
                    trade_count INTEGER NOT NULL,
                    computed_at TEXT NOT NULL,
                    window_days INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS trades (
                    paging_token TEXT PRIMARY KEY,
                    base_asset_code TEXT,
                    counter_asset_code TEXT,
                    base_amount REAL,
                    ledger_close_time TEXT
                )"""
            )
            conn.commit()

        calibrator = BenfordBaselineCalibrator(db_path=db)

        # First calibration: just the theoretical fallback since table is empty
        baseline1 = calibrator.calibrate("XLM/USDC", window_days=30)
        assert baseline1.trade_count == 0

        # Add some trades
        now = datetime.now(timezone.utc)
        with sqlite3.connect(db) as conn:
            for i in range(1, 11):
                conn.execute(
                    """INSERT INTO trades
                       (paging_token, base_asset_code, counter_asset_code,
                        base_amount, ledger_close_time)
                       VALUES (?, ?, ?, ?, ?)""",
                    (f"tok2_{i}", "XLM", "USDC", float(i * 100), now.isoformat()),
                )
            conn.commit()

        baseline2 = calibrator.calibrate("XLM/USDC", window_days=30)
        assert baseline2.trade_count == 10  # overwritten

        # Verify only one row exists in the table
        with sqlite3.connect(db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM benford_baselines WHERE asset_pair = ?", ("XLM/USDC",)
            ).fetchone()[0]
            assert count == 1


class TestBenfordBaselineLookup:
    """Tests for BenfordBaseline calibrator using the load method."""

    def test_load_returns_correct_structure(self, tmp_path):
        """load returns a BenfordBaseline with all fields populated."""
        db = str(tmp_path / "test.db")
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS benford_baselines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_pair TEXT NOT NULL,
                    digit_freqs_json TEXT NOT NULL,
                    trade_count INTEGER NOT NULL,
                    computed_at TEXT NOT NULL,
                    window_days INTEGER NOT NULL
                )"""
            )
            freqs = [0.301, 0.176, 0.125, 0.097, 0.079, 0.067, 0.058, 0.051, 0.046]
            computed = datetime.now(timezone.utc)
            conn.execute(
                """INSERT INTO benford_baselines
                   (asset_pair, digit_freqs_json, trade_count, computed_at, window_days)
                   VALUES (?, ?, ?, ?, ?)""",
                ("XLM/ETH", json.dumps(freqs), 500, computed.isoformat(), 7),
            )
            conn.commit()

        calibrator = BenfordBaselineCalibrator(db_path=db)
        baseline = calibrator.load("XLM/ETH")

        assert baseline is not None
        assert isinstance(baseline, BenfordBaseline)
        assert baseline.asset_pair == "XLM/ETH"
        assert baseline.digit_freqs == freqs
        assert baseline.trade_count == 500
        assert baseline.window_days == 7
        assert baseline.computed_at == computed
