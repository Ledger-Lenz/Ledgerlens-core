"""AMM liquidity-pool manipulation features and wash-trading session detection.

A swap against a pool has no counterparty wallet, so the classic
counterparty-concentration / round-trip features in
`detection.feature_engineering` can't see pool-routed wash volume. These
functions operate on `Trade` rows with `trade_type=LIQUIDITY_POOL` (see
`ingestion.data_models.TradeType`) instead.

The ``AMMEngine`` class detects the three-phase AMM wash-trading pattern:
deposit liquidity -> execute self-dealing trades -> withdraw liquidity within
a short window, leaving no net economic exposure but producing fraudulent
volume figures.
"""

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from detection.sandwich_engine import detect_sandwich_candidates
from ingestion.data_models import LiquidityPool, TradeType

logger = logging.getLogger("ledgerlens.amm_engine")

_POOL_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_SESSIONS_PER_WALLET = 1000


@dataclass
class AMMSession:
    wallet: str
    pool_id: str
    deposit_time: datetime
    withdraw_time: Optional[datetime]
    deposited_amount_a: float
    deposited_amount_b: float
    withdrawn_amount_a: float
    withdrawn_amount_b: float
    trades_during_tenure: list[dict] = field(default_factory=list)

    @property
    def tenure_seconds(self) -> float:
        if self.withdraw_time is None:
            return float("inf")
        return (self.withdraw_time - self.deposit_time).total_seconds()

    @property
    def volume_to_liquidity_ratio(self) -> float:
        liquidity = max(self.deposited_amount_a + self.deposited_amount_b, 1e-9)
        volume = sum(t.get("base_amount", 0) for t in self.trades_during_tenure)
        return volume / liquidity

    @property
    def deposit_withdraw_symmetry(self) -> float:
        """0.0 = asymmetric (genuine LP), 1.0 = perfectly symmetric (suspicious)."""
        delta_a = abs(self.deposited_amount_a - self.withdrawn_amount_a)
        delta_b = abs(self.deposited_amount_b - self.withdrawn_amount_b)
        norm = max(self.deposited_amount_a + self.deposited_amount_b, 1e-9)
        return 1.0 - min((delta_a + delta_b) / norm, 1.0)


@dataclass
class AMMPoolAnomaly:
    wallet: str
    pool_id: str
    session_start: datetime
    tenure_seconds: float
    volume_to_liquidity_ratio: float
    deposit_withdraw_symmetry: float
    counterparty_concentration: float
    anomaly_score: float
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _sanitize_log_str(s: str, max_len: int = 100) -> str:
    """Strip newlines and truncate for safe log output."""
    return s.replace("\n", "").replace("\r", "")[:max_len]


class AMMEngine:
    """Detect AMM wash-trading sessions from liquidity pool operations."""

    def __init__(
        self,
        max_tenure_seconds: float = float(os.getenv("AMM_MAX_TENURE_SECONDS", "14400")),
        min_volume_ratio: float = float(os.getenv("AMM_MIN_VOLUME_RATIO", "5.0")),
        min_symmetry: float = float(os.getenv("AMM_MIN_SYMMETRY", "0.85")),
        min_counterparty_concentration: float = float(os.getenv("AMM_MIN_COUNTERPARTY_CONCENTRATION", "0.7")),
    ):
        self.max_tenure_seconds = max_tenure_seconds
        self.min_volume_ratio = min_volume_ratio
        self.min_symmetry = min_symmetry
        self.min_counterparty_concentration = min_counterparty_concentration
        self._sessions: dict[tuple[str, str], list[AMMSession]] = defaultdict(list)
        self._anomalies: list[AMMPoolAnomaly] = []
        self._seen_keys: set[tuple[str, str, str]] = set()

    def _compute_counterparty_concentration(self, trades: list[dict]) -> float:
        if not trades:
            return 0.0
        counterparties: dict[str, float] = defaultdict(float)
        for t in trades:
            cp = t.get("counter_account") or t.get("counterparty", "pool")
            counterparties[cp] += abs(t.get("base_amount", 0))
        total = sum(counterparties.values())
        if total <= 0:
            return 0.0
        return max(counterparties.values()) / total

    def _score_session(self, session: AMMSession) -> float:
        """Compute composite anomaly score in [0, 1]. Monotone in all sub-signals."""
        scores = []

        # Tenure: shorter = more suspicious
        if session.tenure_seconds <= self.max_tenure_seconds:
            tenure_score = 1.0 - (session.tenure_seconds / max(self.max_tenure_seconds, 1))
        else:
            tenure_score = 0.0
        scores.append(tenure_score)

        # Volume ratio
        vol_score = min(session.volume_to_liquidity_ratio / max(self.min_volume_ratio * 4, 1), 1.0)
        scores.append(vol_score)

        # Symmetry
        scores.append(session.deposit_withdraw_symmetry)

        # Counterparty concentration
        cp_conc = self._compute_counterparty_concentration(session.trades_during_tenure)
        scores.append(cp_conc)

        return sum(scores) / len(scores)

    def ingest_operations(
        self,
        operations: list[dict],
        trades: list[dict],
    ) -> list[AMMPoolAnomaly]:
        """Build sessions from AMM operations, score them, return anomalies.

        Idempotent: calling twice with the same data produces no duplicates.
        """
        # Sort operations by paging_token / timestamp for ordering invariant
        operations = sorted(operations, key=lambda o: o.get("paging_token", o.get("timestamp", "")))

        for op in operations:
            wallet = _sanitize_log_str(op.get("source_account", ""))
            pool_id = op.get("pool_id", op.get("liquidity_pool_id", ""))

            if pool_id and not _POOL_ID_PATTERN.match(pool_id):
                continue

            key = (wallet, pool_id)
            op_type = op.get("type", "")

            if op_type in ("liquidity_pool_deposit", "deposit"):
                session = AMMSession(
                    wallet=wallet,
                    pool_id=pool_id,
                    deposit_time=_parse_time(op.get("timestamp", op.get("created_at", ""))),
                    withdraw_time=None,
                    deposited_amount_a=float(op.get("amount_a", op.get("reserves_deposited", [{}])[0].get("amount", 0) if isinstance(op.get("reserves_deposited"), list) else 0)),
                    deposited_amount_b=float(op.get("amount_b", op.get("reserves_deposited", [{}])[-1].get("amount", 0) if isinstance(op.get("reserves_deposited"), list) else 0)),
                    withdrawn_amount_a=0.0,
                    withdrawn_amount_b=0.0,
                )
                if len(self._sessions[key]) < MAX_SESSIONS_PER_WALLET:
                    self._sessions[key].append(session)

            elif op_type in ("liquidity_pool_withdraw", "withdraw"):
                sessions = self._sessions.get(key, [])
                open_sessions = [s for s in sessions if s.withdraw_time is None]
                if open_sessions:
                    session = open_sessions[0]
                    session.withdraw_time = _parse_time(op.get("timestamp", op.get("created_at", "")))
                    session.withdrawn_amount_a = float(op.get("amount_a", op.get("reserves_received", [{}])[0].get("amount", 0) if isinstance(op.get("reserves_received"), list) else 0))
                    session.withdrawn_amount_b = float(op.get("amount_b", op.get("reserves_received", [{}])[-1].get("amount", 0) if isinstance(op.get("reserves_received"), list) else 0))

        # Assign trades to sessions
        for trade in trades:
            wallet = trade.get("base_account", "")
            pool_id = trade.get("liquidity_pool_id", "")
            if not pool_id:
                continue
            key = (wallet, pool_id)
            trade_time = _parse_time(trade.get("ledger_close_time", trade.get("timestamp", "")))
            for session in self._sessions.get(key, []):
                if session.deposit_time <= trade_time:
                    if session.withdraw_time is None or trade_time <= session.withdraw_time:
                        session.trades_during_tenure.append(trade)

        # Score completed sessions
        anomalies: list[AMMPoolAnomaly] = []
        for key, sessions in self._sessions.items():
            for session in sessions:
                if session.withdraw_time is None:
                    continue

                dedup_key = (session.wallet, session.pool_id, session.deposit_time.isoformat())
                if dedup_key in self._seen_keys:
                    continue
                self._seen_keys.add(dedup_key)

                score = self._score_session(session)
                cp_conc = self._compute_counterparty_concentration(session.trades_during_tenure)

                anomaly = AMMPoolAnomaly(
                    wallet=session.wallet,
                    pool_id=session.pool_id,
                    session_start=session.deposit_time,
                    tenure_seconds=session.tenure_seconds,
                    volume_to_liquidity_ratio=session.volume_to_liquidity_ratio,
                    deposit_withdraw_symmetry=session.deposit_withdraw_symmetry,
                    counterparty_concentration=cp_conc,
                    anomaly_score=score,
                )
                if score > 0.3:
                    anomalies.append(anomaly)
                    self._anomalies.append(anomaly)

        return anomalies

    def get_features(self, wallet: str) -> dict[str, float]:
        """Return AMM session features for a wallet. Cold-start safe."""
        wallet_sessions = []
        for key, sessions in self._sessions.items():
            if key[0] == wallet:
                wallet_sessions.extend(sessions)

        if not wallet_sessions:
            return {"amm_tenure_ratio": 0.0, "amm_volume_concentration": 0.0}

        completed = [s for s in wallet_sessions if s.withdraw_time is not None]
        if not completed:
            return {"amm_tenure_ratio": 0.0, "amm_volume_concentration": 0.0}

        short_tenure = sum(1 for s in completed if s.tenure_seconds <= self.max_tenure_seconds)
        tenure_ratio = short_tenure / len(completed)

        vol_ratios = [s.volume_to_liquidity_ratio for s in completed]
        max_vol_conc = max(vol_ratios) if vol_ratios else 0.0

        return {
            "amm_tenure_ratio": float(tenure_ratio),
            "amm_volume_concentration": float(min(max_vol_conc / 20.0, 1.0)),
        }

    def get_anomalies(self, min_score: float = 0.5) -> list[AMMPoolAnomaly]:
        """Return stored anomalies above the minimum score."""
        return [a for a in self._anomalies if a.anomaly_score >= min_score]


def _parse_time(ts) -> datetime:
    """Parse a timestamp string or return it if already a datetime."""
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, pd.Timestamp):
        return ts.to_pydatetime()
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _pair_key(row: pd.Series) -> tuple:
    base = row["base_asset"]
    counter = row["counter_asset"]
    return (base.get("code"), base.get("issuer"), counter.get("code"), counter.get("issuer"))


def pool_round_trip_ratio(
    trades: pd.DataFrame,
    account: str,
    pool_id: str,
    window: pd.Timedelta = pd.Timedelta(hours=1),
) -> float:
    """Fraction of an account's pool trades that are a buy followed by a sell
    of the same asset pair within `window` — a proxy for using pool swaps to
    manufacture volume without real price exposure.
    """
    if trades.empty or "trade_type" not in trades.columns:
        return 0.0

    mask = (
        (trades["trade_type"] == TradeType.LIQUIDITY_POOL)
        & (trades["liquidity_pool_id"] == pool_id)
        & (trades["base_account"] == account)
    )
    pool_trades = trades.loc[mask].sort_values("ledger_close_time").reset_index(drop=True)
    n = len(pool_trades)
    if n < 2:
        return 0.0

    round_trips = 0
    for i in range(n):
        row_i = pool_trades.iloc[i]
        pair_i = _pair_key(row_i)
        window_end = row_i["ledger_close_time"] + window
        later = pool_trades.iloc[i + 1 :]
        later = later[later["ledger_close_time"] <= window_end]
        for _, row_j in later.iterrows():
            if _pair_key(row_j) == pair_i and row_j["base_is_seller"] != row_i["base_is_seller"]:
                round_trips += 1
                break

    return round_trips / n


def pool_sandwich_count(
    trades: pd.DataFrame,
    pool_id: str,
    min_profit_xlm: float = 10.0,
    max_ledger_gap: int = 2,
) -> int:
    """Number of sandwich-attack candidates detected against `pool_id`.

    Operates on the same `Trade`-shaped DataFrame as `pool_round_trip_ratio`
    (rows with `trade_type == LIQUIDITY_POOL`). Returns 0 when the pool has no
    trades or the schema lacks the price/direction columns the detector needs.
    """
    if trades.empty or "liquidity_pool_id" not in trades.columns:
        return 0

    pool_trades = trades.loc[trades["liquidity_pool_id"] == pool_id]
    if pool_trades.empty:
        return 0

    return len(
        detect_sandwich_candidates(
            pool_trades,
            min_profit_xlm=min_profit_xlm,
            max_ledger_gap=max_ledger_gap,
        )
    )


def pool_sandwich_frequency(
    trades: pd.DataFrame,
    pool_id: str,
    min_profit_xlm: float = 10.0,
    max_ledger_gap: int = 2,
) -> float:
    """Fraction of `pool_id`'s trades that participate in a detected sandwich.

    Each candidate consumes three trade legs (buy, victim, sell); the ratio is
    `3 * candidate_count / pool_trade_count`, clamped to 1.0. A pool-level
    proxy for how heavily a pool is being sandwiched.
    """
    if trades.empty or "liquidity_pool_id" not in trades.columns:
        return 0.0

    pool_trades = trades.loc[trades["liquidity_pool_id"] == pool_id]
    n = len(pool_trades)
    if n == 0:
        return 0.0

    count = pool_sandwich_count(trades, pool_id, min_profit_xlm, max_ledger_gap)
    return float(min(3 * count / n, 1.0))


def pool_share_concentration(pool: LiquidityPool, deposits: pd.DataFrame) -> float:
    """Herfindahl-style concentration of `pool`'s deposit/withdraw activity
    across accounts — flags a single actor inflating then draining a pool to
    move its price around their own trades.

    `deposits` must have `account` and `amount` columns.
    """
    if deposits.empty:
        return 0.0

    volumes = deposits.groupby("account")["amount"].sum().abs()
    total = volumes.sum()
    if total <= 0:
        return 0.0

    shares = volumes / total
    return float((shares**2).sum())


def pool_risk_from_trade_rows(rows: list[dict], window: pd.Timedelta = pd.Timedelta(hours=1)) -> dict:
    """Aggregate round-trip ratio and trader concentration from stored pool
    trade rows (`detection.storage.get_liquidity_pool_trades`'s shape:
    `base_account`, `base_asset_pair`, `counter_asset_pair`, `base_amount`,
    `base_is_seller`, `timestamp`).

    Used by the `/amm/pools/{pool_id}/risk` API endpoint, where trades have
    already been flattened to scalar columns rather than the nested `Trade`
    schema `pool_round_trip_ratio` expects.
    """
    if not rows:
        return {"round_trip_ratio": 0.0, "trader_concentration": 0.0, "trade_count": 0}

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    volumes = df.groupby("base_account")["base_amount"].sum()
    total_volume = volumes.sum()
    trader_concentration = float(((volumes / total_volume) ** 2).sum()) if total_volume > 0 else 0.0

    round_trips = 0
    for account, account_df in df.groupby("base_account"):
        account_df = account_df.sort_values("timestamp").reset_index(drop=True)
        n = len(account_df)
        for i in range(n):
            row_i = account_df.iloc[i]
            window_end = row_i["timestamp"] + window
            later = account_df.iloc[i + 1 :]
            later = later[later["timestamp"] <= window_end]
            matched = later[
                (later["base_asset_pair"] == row_i["base_asset_pair"])
                & (later["counter_asset_pair"] == row_i["counter_asset_pair"])
                & (later["base_is_seller"] != row_i["base_is_seller"])
            ]
            if not matched.empty:
                round_trips += 1

    return {
        "round_trip_ratio": float(round_trips / len(df)),
        "trader_concentration": trader_concentration,
        "trade_count": int(len(df)),
    }
