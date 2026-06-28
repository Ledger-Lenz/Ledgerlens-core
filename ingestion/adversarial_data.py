"""Evasion-aware wash-ring generator for adversarial robustness testing.

Extends `ingestion.synthetic_data` with wash rings that actively try to
evade the detection features in `detection.feature_engineering`.  Each
evasion strategy targets a specific detection signal:

- ``benford_mimicry``       – amounts drawn from a lognormal fitted to
                              natural trade data so digit distribution
                              matches Benford's Law expectation.
- ``temporal_jitter``       – 60–180 s delay between round-trip legs to
                              avoid tight intra-minute clustering.
- ``hour_spread``           – trades spread uniformly across all 24 h to
                              dilute the off-hours activity ratio.
- ``counterparty_rotation`` – each wash trade uses a fresh wallet from a
                              pool of 20+ to suppress counterparty
                              concentration.
- ``decoy_trades``          – 6 low-value legitimate-looking trades
                              inserted between each wash pair to mask
                              the round-trip pattern.
"""

import random
import string
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ingestion.data_models import Trade
from ingestion.synthetic_data import (
    _make_trade,
    _random_account,
    generate_synthetic_dataset,
)

ALL_STRATEGIES = [
    "benford_mimicry",
    "temporal_jitter",
    "hour_spread",
    "counterparty_rotation",
    "decoy_trades",
]

_ADDRESS_ALPHABET = string.ascii_uppercase + "234567"


def generate_adversarial_dataset(
    n_normal_accounts: int = 50,
    n_wash_rings: int = 10,
    ring_size: int = 4,
    evasion_strategies: list[str] | None = None,
    seed: int = 42,
) -> tuple[pd.DataFrame, dict, pd.DataFrame, dict[str, int]]:
    """Generate wash-trading rings that actively attempt to evade detection.

    Parameters
    ----------
    evasion_strategies:
        Subset of ``ALL_STRATEGIES`` to apply; ``None`` activates all five.

    Returns
    -------
    Same four-tuple as `generate_synthetic_dataset`:
    ``(trades_df, account_metadata, events_df, labels)``.
    """
    strategies = set(evasion_strategies if evasion_strategies is not None else ALL_STRATEGIES)
    unknown = strategies - set(ALL_STRATEGIES)
    if unknown:
        raise ValueError(f"Unknown evasion strategies: {unknown}")

    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    as_of = datetime.now(timezone.utc)
    lookback_days = 30
    start = as_of - timedelta(days=lookback_days)

    # --- normal accounts (same as synthetic_data baseline) ---
    normal_trades_df, account_metadata, normal_events_df, labels = generate_synthetic_dataset(
        n_normal_accounts=n_normal_accounts,
        n_wash_rings=0,
        ring_size=ring_size,
        lookback_days=lookback_days,
        as_of=as_of,
        seed=seed,
    )

    # Pool of decoy counterparties used for counterparty_rotation
    rotation_pool = [_random_account(rng) for _ in range(20)]
    for acc in rotation_pool:
        labels[acc] = 0  # not wash-labelled; they're innocent bystanders
        account_metadata[acc] = {
            "funding_source": _random_account(rng),
            "created_at": start - timedelta(days=rng.uniform(30, 365)),
        }

    wash_trades: list[Trade] = []
    trade_id = int(normal_trades_df["id"].astype(int).max()) + 1 if not normal_trades_df.empty else 1

    for _ in range(n_wash_rings):
        ring_accounts = [_random_account(rng) for _ in range(ring_size)]
        shared_funder = _random_account(rng)
        ring_created = as_of - timedelta(days=rng.uniform(0, 5))

        for acc in ring_accounts:
            labels[acc] = 1
            account_metadata[acc] = {
                "funding_source": shared_funder,
                "created_at": ring_created,
            }

        trades_per_wash = 30
        for _ in range(trades_per_wash):
            # --- pick base time ---
            base_time = start + timedelta(seconds=rng.uniform(0, lookback_days * 86400))

            if "hour_spread" in strategies:
                # Spread uniformly over all 24 hours instead of clustering off-hours
                base_time = base_time.replace(hour=rng.randint(0, 23))
            else:
                base_time = base_time.replace(hour=rng.randint(0, 5))

            # --- pick amount ---
            if "benford_mimicry" in strategies:
                # Sample from lognormal that mimics natural trade amounts
                amount = float(np_rng.lognormal(mean=3.0, sigma=1.5))
            else:
                amount = float(rng.choice((100.0, 200.0, 500.0, 1000.0)))

            price = float(np_rng.uniform(0.08, 0.15))

            # --- pick counterparty ---
            if "counterparty_rotation" in strategies:
                base_account = rng.choice(ring_accounts)
                counter_account = rng.choice([a for a in rotation_pool if a != base_account])
            else:
                base_account, counter_account = rng.sample(ring_accounts, 2)

            # --- decoy trades before the wash pair ---
            if "decoy_trades" in strategies:
                for d in range(6):
                    decoy_time = base_time - timedelta(seconds=rng.uniform(60, 600))
                    decoy_amount = float(np_rng.lognormal(mean=1.5, sigma=0.8))
                    decoy_counterparty = rng.choice(rotation_pool)
                    wash_trades.append(
                        _make_trade(trade_id, decoy_time, base_account, decoy_counterparty, decoy_amount, price)
                    )
                    trade_id += 1

            # --- wash trade leg 1 ---
            wash_trades.append(_make_trade(trade_id, base_time, base_account, counter_account, amount, price))
            trade_id += 1

            # --- round-trip leg ---
            if "temporal_jitter" in strategies:
                jitter = rng.uniform(60, 180)
            else:
                jitter = rng.uniform(1, 60)
            return_time = base_time + timedelta(seconds=jitter)
            wash_trades.append(_make_trade(trade_id, return_time, counter_account, base_account, amount, price))
            trade_id += 1

    wash_trades_df = pd.DataFrame([t.model_dump() for t in wash_trades])
    wash_trades_df["ledger_close_time"] = pd.to_datetime(wash_trades_df["ledger_close_time"], utc=True)

    wash_events_df = pd.DataFrame(columns=["id", "timestamp", "account", "asset_pair", "side", "amount", "price", "event_type"])

    trades_df = pd.concat([normal_trades_df, wash_trades_df], ignore_index=True)
    trades_df = trades_df.sort_values("ledger_close_time").reset_index(drop=True)

    events_df = pd.concat([normal_events_df, wash_events_df], ignore_index=True)

    return trades_df, account_metadata, events_df, labels


# ============================================================
# Specialist adversarial generators (new evasion strategies)
# ============================================================

from typing import Literal  # noqa: E402

from ingestion.data_models import Asset  # noqa: E402
from ingestion.synthetic_data import NATIVE, USDC  # noqa: E402

BENFORD_PROBS = np.array([np.log10(1 + 1 / d) for d in range(1, 10)])
BENFORD_PROBS /= BENFORD_PROBS.sum()

ASSET_PAIRS = ["XLM/USDC", "XLM/yXLM", "USDC/yUSDC", "XLM/AQUA", "USDC/AQUA"]

_ASSET_ISSUERS: dict[str, str | None] = {
    "XLM": None,
    "USDC": "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
    "yXLM": "GARDNV3Q7YGT4AKSDF25LT32YSCCW4EV22Y2TV3I2PU2MMXJTEDL5T55",
    "yUSDC": "GDGTVWSM4MGS4T7Z6W4RPWOCHE2I6RDFCIFZGS3DOA63LWQTRNZNTTTH",
    "AQUA": "GBDYDBJKQBJK4GY4V7FAONSFF2IBJSKNTBYJ65F5KCGBY2BIGPGGLJOH",
}

_BASE32_CHARS = frozenset(string.ascii_uppercase + "234567")


def _resolve_pair(pair_str: str) -> tuple[Asset, Asset]:
    """Parse 'BASE/COUNTER' into (base Asset, counter Asset) with synthetic issuers."""
    base_code, counter_code = pair_str.split("/")
    base_issuer = _ASSET_ISSUERS.get(base_code)
    counter_issuer = _ASSET_ISSUERS.get(counter_code)
    if base_code != "XLM" and base_issuer is None:
        raise ValueError(f"No issuer defined for asset code {base_code!r}")
    if counter_code != "XLM" and counter_issuer is None:
        raise ValueError(f"No issuer defined for asset code {counter_code!r}")
    return (
        Asset(code=base_code, issuer=base_issuer),
        Asset(code=counter_code, issuer=counter_issuer),
    )


def _is_valid_stellar_address(addr: str) -> bool:
    """Return True if addr looks like a valid 56-char Stellar G-address."""
    return (
        len(addr) == 56
        and addr[0] == "G"
        and all(c in _BASE32_CHARS for c in addr[1:])
    )


class BenfordCamouflageGenerator:
    """Generate wash trades with amounts that conform to Benford's Law.

    Samples leading digit d ~ Benford(d), then draws amount uniformly within
    the leading-digit bucket [d*10^k, (d+1)*10^k) for a random order of
    magnitude k.  The wash relationship (same wallet ring, coordinated
    round-trip pattern) remains intact while the digit distribution is
    engineered to pass Benford chi-square / MAD checks.
    """

    def __init__(
        self,
        min_order: int = 2,
        max_order: int = 5,
        seed: int | None = None,
    ) -> None:
        self.rng = np.random.default_rng(seed)
        self.min_order = min_order
        self.max_order = max_order

    def sample_amount(self) -> float:
        """Sample one Benford-conforming amount, floored at 1e-7."""
        d = int(self.rng.choice(9, p=BENFORD_PROBS)) + 1  # digit 1–9
        k = int(self.rng.integers(self.min_order, self.max_order + 1))
        base = d * (10 ** k)
        amount = base + self.rng.uniform(0, (10 ** k) - 1)
        return max(round(float(amount), 7), 1e-7)

    def generate(
        self,
        wallets: list[str],
        n_trades: int,
        asset_pair: str = "XLM/USDC",
        start_time: datetime | None = None,
    ) -> list[Trade]:
        """Generate n_trades wash trades between wallets with Benford-conforming amounts."""
        if start_time is None:
            start_time = datetime(2025, 12, 1, tzinfo=timezone.utc)
        base_asset, counter_asset = _resolve_pair(asset_pair)
        price = float(self.rng.uniform(0.08, 0.15))
        trades = []
        for i in range(n_trades):
            seller = wallets[i % len(wallets)]
            buyer = wallets[(i + 1) % len(wallets)]
            amount = self.sample_amount()
            ts = start_time + timedelta(seconds=int(self.rng.integers(0, 30 * 86400)))
            trades.append(Trade(
                id=str(i + 1),
                ledger_close_time=ts,
                base_account=seller,
                counter_account=buyer,
                base_asset=base_asset,
                counter_asset=counter_asset,
                base_amount=amount,
                counter_amount=max(round(amount * price, 7), 1e-7),
                price=price,
                base_is_seller=i % 2 == 0,
            ))
        return trades


class TimingJitterGenerator:
    """Generate wash trades with Poisson-process inter-arrival times.

    A Poisson process has exponentially distributed inter-arrival times with
    mean λ*60 seconds.  Compared to regular or burst-clustered wash trades,
    the Poisson arrivals dilute timing-tightness and intra-minute clustering
    scores while preserving the wash-ring graph structure.
    """

    def __init__(
        self,
        mean_interval_minutes: float = 10.0,
        seed: int | None = None,
    ) -> None:
        self.lam = mean_interval_minutes
        self.rng = np.random.default_rng(seed)

    def generate_timestamps(
        self, n_trades: int, start_time: datetime
    ) -> list[datetime]:
        """Return n_trades timestamps with Exponential(λ) inter-arrival gaps.

        Inter-arrival times are drawn from Exponential(mean=λ*60s), the
        marginal distribution of a Poisson process with rate 1/λ per minute.
        """
        intervals = self.rng.exponential(scale=self.lam * 60, size=n_trades)
        timestamps = [start_time]
        for interval in intervals[1:]:
            timestamps.append(timestamps[-1] + timedelta(seconds=float(interval)))
        return timestamps

    def generate(
        self,
        wallets: list[str],
        n_trades: int,
        start_time: datetime | None = None,
    ) -> list[Trade]:
        """Generate n_trades wash trades with Poisson inter-arrival times."""
        if start_time is None:
            start_time = datetime(2025, 12, 1, tzinfo=timezone.utc)
        timestamps = self.generate_timestamps(n_trades, start_time)
        price = float(self.rng.uniform(0.08, 0.15))
        trades = []
        for i, ts in enumerate(timestamps):
            seller = wallets[i % len(wallets)]
            buyer = wallets[(i + 1) % len(wallets)]
            amount = max(float(self.rng.uniform(100, 5_000)), 1e-7)
            trades.append(Trade(
                id=str(i + 1),
                ledger_close_time=ts,
                base_account=seller,
                counter_account=buyer,
                base_asset=NATIVE,
                counter_asset=USDC,
                base_amount=amount,
                counter_amount=max(round(amount * price, 7), 1e-7),
                price=price,
                base_is_seller=i % 2 == 0,
            ))
        return trades


class GraphFragmentationGenerator:
    """Break wash activity into multiple isolated 3-node SCCs to evade ring-size limits.

    Creates ``n_hub_wallets // 3`` separate 3-wallet rings.  Each ring is a
    closed directed cycle (A→B→C→A), forming a strongly connected component
    of exactly 3 nodes — well below the ``MAX_RING_SIZE=10`` threshold for
    full ring enumeration.  Rings are disconnected from each other so no
    individual SCC exceeds 3 nodes.

    All generated wallets carry the ``GFRAG`` prefix followed by 56 decimal
    digits, making them 61 characters long.  Digits 0, 1, 8, 9 are absent
    from the Stellar base-32 alphabet, so GFRAG addresses can never pass
    Stellar G-address validation.
    """

    def generate(
        self,
        n_hub_wallets: int = 12,
        n_trades_per_fragment: int = 20,
        seed: int | None = None,
        start_time: datetime | None = None,
    ) -> list[Trade]:
        """Create isolated 3-node wash rings using GFRAG-prefixed synthetic wallets.

        Raises
        ------
        ValueError
            If any generated address passes the Stellar G-address length/charset check.
        """
        rng = np.random.default_rng(seed)
        if start_time is None:
            start_time = datetime(2025, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

        n_rings = max(1, n_hub_wallets // 3)
        wallets = [f"GFRAG{i:056d}" for i in range(n_rings * 3)]

        for addr in wallets:
            if _is_valid_stellar_address(addr):
                raise ValueError(
                    f"Generated GFRAG address {addr!r} passes Stellar G-address "
                    "validation; GFRAG addresses must be clearly synthetic."
                )

        price = float(rng.uniform(0.08, 0.15))
        trades = []
        trade_id = 0

        for ring_idx in range(n_rings):
            ring = wallets[ring_idx * 3 : ring_idx * 3 + 3]
            for j in range(n_trades_per_fragment):
                seller = ring[j % 3]
                buyer = ring[(j + 1) % 3]
                amount = max(float(rng.uniform(100, 10_000)), 1e-7)
                # Offset each ring 2 days apart; trades within a ring every 10 minutes
                ts_offset = ring_idx * 2 * 86_400 + j * 600
                ts = start_time + timedelta(seconds=ts_offset)
                trade_id += 1
                trades.append(Trade(
                    id=str(trade_id),
                    ledger_close_time=ts,
                    base_account=seller,
                    counter_account=buyer,
                    base_asset=NATIVE,
                    counter_asset=USDC,
                    base_amount=amount,
                    counter_amount=max(round(amount * price, 7), 1e-7),
                    price=price,
                    base_is_seller=j % 2 == 0,
                ))

        return trades


class CrossPairRotationGenerator:
    """Rotate wash volume across multiple asset pairs per time window.

    Distributes the same wallet ring's activity across ``ASSET_PAIRS``
    (XLM/USDC, XLM/yXLM, USDC/yUSDC, XLM/AQUA, USDC/AQUA), so no single
    pair shows concentrated volume — evading per-pair detection while
    preserving the ring graph structure that is visible across all pairs.
    """

    def generate(
        self,
        wallets: list[str],
        n_trades_per_pair: int = 30,
        pairs: list[str] | None = None,
        seed: int | None = None,
        start_time: datetime | None = None,
    ) -> list[Trade]:
        """Generate wash trades rotated across all asset pairs."""
        if pairs is None:
            pairs = ASSET_PAIRS
        if start_time is None:
            start_time = datetime(2025, 12, 1, tzinfo=timezone.utc)

        rng = np.random.default_rng(seed)
        trades = []
        trade_id = 0

        for pair_idx, pair in enumerate(pairs):
            base_asset, counter_asset = _resolve_pair(pair)
            price = float(rng.uniform(0.08, 0.15))
            pair_start = start_time + timedelta(days=pair_idx * 6)
            for i in range(n_trades_per_pair):
                seller = wallets[i % len(wallets)]
                buyer = wallets[(i + 1) % len(wallets)]
                amount = max(float(rng.uniform(50, 5_000)), 1e-7)
                ts = pair_start + timedelta(seconds=int(rng.integers(0, 6 * 86_400)))
                trade_id += 1
                trades.append(Trade(
                    id=str(trade_id),
                    ledger_close_time=ts,
                    base_account=seller,
                    counter_account=buyer,
                    base_asset=base_asset,
                    counter_asset=counter_asset,
                    base_amount=amount,
                    counter_amount=max(round(amount * price, 7), 1e-7),
                    price=price,
                    base_is_seller=i % 2 == 0,
                ))

        return trades


_ADV_STRATEGIES = frozenset(
    ["benford_camouflage", "timing_jitter", "graph_fragmentation", "cross_pair_rotation"]
)


class AdversarialDataset:
    """Build labelled feature DataFrames for adversarial recall evaluation.

    Combines normal-background trades (from ``generate_synthetic_dataset``) with
    evasion-strategy wash trades from the specialist generators, then runs the
    full LedgerLens feature pipeline (``build_training_dataset``) to produce a
    :class:`~pandas.DataFrame` with ``FEATURE_NAMES`` columns plus a ``label``
    column (0 = normal, 1 = adversarial wash).
    """

    def build(
        self,
        strategy: Literal[
            "benford_camouflage",
            "timing_jitter",
            "graph_fragmentation",
            "cross_pair_rotation",
        ],
        n_wallets: int = 50,
        n_trades: int = 200,
        seed: int = 42,
    ) -> pd.DataFrame:
        """Generate a labelled feature dataset for one evasion strategy.

        Parameters
        ----------
        strategy:
            Which evasion generator to use.
        n_wallets:
            Number of adversarial wash wallets (approximately; graph_fragmentation
            rounds down to the nearest multiple of 3).
        n_trades:
            Target number of adversarial trades to generate.
        seed:
            Reproducibility seed.

        Returns
        -------
        DataFrame with ``FEATURE_NAMES`` columns + ``label`` column.
        Adversarial wash accounts have ``label=1``; normal background accounts
        have ``label=0``.
        """
        from detection.dataset import build_training_dataset

        if strategy not in _ADV_STRATEGIES:
            raise ValueError(
                f"Unknown strategy {strategy!r}. Choose from {sorted(_ADV_STRATEGIES)}"
            )

        # Use a distinct seed offset for wash-wallet generation so that the
        # random sequence never collides with generate_synthetic_dataset, which
        # also initialises random.Random(seed) internally.
        rng = random.Random((seed + 98_765) % (2 ** 32))
        as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
        start = as_of - timedelta(days=30)

        bg_trades, bg_meta, _events, bg_labels = generate_synthetic_dataset(
            n_normal_accounts=max(5, n_wallets // 2),
            n_wash_rings=0,
            seed=seed,
            as_of=as_of,
        )

        if strategy == "benford_camouflage":
            wallets = [_random_account(rng) for _ in range(n_wallets)]
            adv_trades = BenfordCamouflageGenerator(seed=seed).generate(
                wallets, n_trades, start_time=start
            )
            wash_wallets = wallets

        elif strategy == "timing_jitter":
            wallets = [_random_account(rng) for _ in range(n_wallets)]
            adv_trades = TimingJitterGenerator(seed=seed).generate(
                wallets, n_trades, start_time=start
            )
            wash_wallets = wallets

        elif strategy == "graph_fragmentation":
            per_frag = max(5, n_trades // max(1, n_wallets // 3))
            adv_trades = GraphFragmentationGenerator().generate(
                n_hub_wallets=n_wallets,
                n_trades_per_fragment=per_frag,
                seed=seed,
                start_time=start,
            )
            wash_wallets = list(
                {t.base_account for t in adv_trades}
                | {t.counter_account for t in adv_trades if t.counter_account}
            )

        else:  # cross_pair_rotation
            wallets = [_random_account(rng) for _ in range(n_wallets)]
            per_pair = max(5, n_trades // len(ASSET_PAIRS))
            adv_trades = CrossPairRotationGenerator().generate(
                wallets, n_trades_per_pair=per_pair, seed=seed, start_time=start
            )
            wash_wallets = wallets

        adv_df = pd.DataFrame([t.model_dump() for t in adv_trades])
        adv_df["ledger_close_time"] = pd.to_datetime(adv_df["ledger_close_time"], utc=True)

        all_trades = pd.concat([bg_trades, adv_df], ignore_index=True)
        all_trades = all_trades.sort_values("ledger_close_time").reset_index(drop=True)
        all_trades["id"] = [str(i + 1) for i in range(len(all_trades))]

        account_metadata = dict(bg_meta)
        shared_funder = _random_account(rng)
        for w in wash_wallets:
            account_metadata[w] = {
                "funding_source": shared_funder,
                "created_at": start - timedelta(days=rng.uniform(0, 30)),
            }

        labels = dict(bg_labels)
        for w in wash_wallets:
            labels[w] = 1

        return build_training_dataset(all_trades, labels, account_metadata=account_metadata)
