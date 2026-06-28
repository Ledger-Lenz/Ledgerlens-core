"""AMM liquidity-pool manipulation features and wash-trade session detection.

A swap against a pool has no counterparty wallet, so the classic
counterparty-concentration / round-trip features in
`detection.feature_engineering` can't see pool-routed wash volume. These
functions operate on `Trade` rows with `trade_type=LIQUIDITY_POOL` (see
`ingestion.data_models.TradeType`) instead.

AMMEngine detects wash trading in AMM pools by identifying the add-liquidity,
trade-burst, remove-liquidity pattern. AMMSession tracks the lifecycle;
AMMPoolAnomaly records scored anomalies.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from detection.sandwich_engine import detect_sandwich_candidates
from ingestion.data_models import LiquidityPool, TradeType

logger = logging.getLogger("ledgerlens.amm_engine")

_POOL_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_SESSIONS_PER_WALLET = 1000


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


@dataclass
class AMMSession:
    wallet: str
    pool_id: str
    deposit_time: datetime
    withdraw_time: datetime | None = None
    deposited_amount_a: float = 0.0
    deposited_amount_b: float = 0.0
    withdrawn_amount_a: float = 0.0
    withdrawn_amount_b: float = 0.0
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
    detected_at: datetime = field(default_factory=datetime.utcnow)


class AMMEngine:
    """Detect AMM wash trading by reconstructing add/trade/remove sessions."""

    def __init__(
        self,
        max_tenure_seconds: float = 14_400,
        min_volume_ratio: float = 5.0,
        min_symmetry: float = 0.85,
        min_counterparty_concentration: float = 0.7,
    ) -> None:
        self.max_tenure_seconds = max_tenure_seconds
        self.min_volume_ratio = min_volume_ratio
        self.min_symmetry = min_symmetry
        self.min_counterparty_concentration = min_counterparty_concentration
        self._sessions: dict[tuple[str, str], list[AMMSession]] = defaultdict(list)
        self._anomalies: list[AMMPoolAnomaly] = []
        self._seen_keys: set[tuple[str, str, str]] = set()

    def ingest_operations(
        self,
        operations: list[dict],
        trades: list[dict],
    ) -> list[AMMPoolAnomaly]:
        """Build sessions from AMM operations, score them, return anomalies."""
        sorted_ops = sorted(operations, key=lambda o: o.get("paging_token", o.get("timestamp", "")))

        for op in sorted_ops:
            wallet = str(op.get("account", op.get("source_account", "")))[:100].replace("\n", "")
            pool_id = str(op.get("liquidity_pool_id", ""))[:100].replace("\n", "")
            if not _POOL_ID_PATTERN.match(pool_id):
                continue

            op_type = op.get("type", op.get("type_i", ""))
            ts = op.get("timestamp", op.get("created_at"))
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
            elif not isinstance(ts, datetime):
                continue

            key = (wallet, pool_id)

            if op_type in ("liquidity_pool_deposit", 22):
                if len(self._sessions[key]) >= MAX_SESSIONS_PER_WALLET:
                    self._sessions[key] = self._sessions[key][-MAX_SESSIONS_PER_WALLET + 1:]
                session = AMMSession(
                    wallet=wallet,
                    pool_id=pool_id,
                    deposit_time=ts,
                    deposited_amount_a=float(op.get("reserves_deposited", [{}])[0].get("amount", 0) if isinstance(op.get("reserves_deposited"), list) else op.get("amount_a", 0)),
                    deposited_amount_b=float(op.get("reserves_deposited", [{}])[-1].get("amount", 0) if isinstance(op.get("reserves_deposited"), list) and len(op.get("reserves_deposited", [])) > 1 else op.get("amount_b", 0)),
                )
                self._sessions[key].append(session)

            elif op_type in ("liquidity_pool_withdraw", 23):
                open_sessions = [s for s in self._sessions[key] if s.withdraw_time is None]
                if open_sessions:
                    session = open_sessions[0]
                    session.withdraw_time = ts
                    session.withdrawn_amount_a = float(op.get("reserves_received", [{}])[0].get("amount", 0) if isinstance(op.get("reserves_received"), list) else op.get("amount_a", 0))
                    session.withdrawn_amount_b = float(op.get("reserves_received", [{}])[-1].get("amount", 0) if isinstance(op.get("reserves_received"), list) and len(op.get("reserves_received", [])) > 1 else op.get("amount_b", 0))

        for trade in trades:
            wallet = str(trade.get("base_account", ""))[:100]
            pool_id = str(trade.get("liquidity_pool_id", ""))[:100]
            if not pool_id or not _POOL_ID_PATTERN.match(pool_id):
                continue
            key = (wallet, pool_id)
            for session in self._sessions.get(key, []):
                trade_ts = trade.get("timestamp", trade.get("ledger_close_time"))
                if isinstance(trade_ts, str):
                    try:
                        trade_ts = datetime.fromisoformat(trade_ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                if trade_ts and trade_ts >= session.deposit_time:
                    if session.withdraw_time is None or trade_ts <= session.withdraw_time:
                        session.trades_during_tenure.append(trade)

        new_anomalies = []
        for key, sessions in self._sessions.items():
            for session in sessions:
                if session.withdraw_time is None:
                    continue

                dedup_key = (session.wallet, session.pool_id, session.deposit_time.isoformat())
                if dedup_key in self._seen_keys:
                    continue

                tenure = session.tenure_seconds
                vol_ratio = session.volume_to_liquidity_ratio
                symmetry = session.deposit_withdraw_symmetry

                counterparties = [t.get("counter_account") for t in session.trades_during_tenure if t.get("counter_account")]
                if counterparties:
                    from collections import Counter
                    cp_counts = Counter(counterparties)
                    cp_conc = max(cp_counts.values()) / len(counterparties)
                else:
                    cp_conc = 0.0

                score = self._compute_anomaly_score(tenure, vol_ratio, symmetry, cp_conc)

                if score > 0.3:
                    anomaly = AMMPoolAnomaly(
                        wallet=session.wallet,
                        pool_id=session.pool_id,
                        session_start=session.deposit_time,
                        tenure_seconds=tenure,
                        volume_to_liquidity_ratio=vol_ratio,
                        deposit_withdraw_symmetry=symmetry,
                        counterparty_concentration=cp_conc,
                        anomaly_score=score,
                    )
                    new_anomalies.append(anomaly)
                    self._anomalies.append(anomaly)
                    self._seen_keys.add(dedup_key)

        return new_anomalies

    def _compute_anomaly_score(
        self, tenure: float, vol_ratio: float, symmetry: float, cp_conc: float
    ) -> float:
        """Composite anomaly score from 4 sub-signals, monotone in each."""
        tenure_score = max(0.0, 1.0 - tenure / self.max_tenure_seconds)
        vol_score = min(1.0, vol_ratio / (self.min_volume_ratio * 4))
        sym_score = max(0.0, (symmetry - 0.5) / 0.5) if symmetry > 0.5 else 0.0
        cp_score = max(0.0, (cp_conc - 0.3) / 0.7) if cp_conc > 0.3 else 0.0
        return min(1.0, 0.3 * tenure_score + 0.3 * vol_score + 0.2 * sym_score + 0.2 * cp_score)

    def get_features(self, wallet: str) -> dict[str, float]:
        """Return AMM features for a wallet."""
        wallet_anomalies = [a for a in self._anomalies if a.wallet == wallet]
        if not wallet_anomalies:
            return {"amm_tenure_ratio": 0.0, "amm_volume_concentration": 0.0}

        avg_tenure_ratio = sum(
            min(1.0, a.tenure_seconds / self.max_tenure_seconds) for a in wallet_anomalies
        ) / len(wallet_anomalies)
        max_vol_conc = max(a.volume_to_liquidity_ratio for a in wallet_anomalies)

        return {
            "amm_tenure_ratio": float(avg_tenure_ratio),
            "amm_volume_concentration": float(min(1.0, max_vol_conc / 20.0)),
        }

    def get_anomalies(
        self, min_score: float = 0.5, limit: int = 100, offset: int = 0
    ) -> list[AMMPoolAnomaly]:
        """Return anomalies sorted by score descending."""
        filtered = [a for a in self._anomalies if a.anomaly_score >= min_score]
        filtered.sort(key=lambda a: a.anomaly_score, reverse=True)
        return filtered[offset : offset + limit]


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
