"""Synthetic SDEX trade data generator for local training and testing.

The canonical labelled dataset lives in `ledgerlens-data`. This module
generates synthetic trade activity for local development:

* A pool of "normal" accounts trading with organic, Benford-conforming
  amounts and Poisson-distributed inter-arrival times.
* Five named **attack profiles** that each simulate a distinct wash-trading
  strategy: round-trip, layering, spoofing, cross-pair, and cross-chain.
* A ``BenfordEvasionMixin`` that adds configurable Gaussian noise to trade
  amounts so a fraction of wash trades appear Benford-compliant, exercising
  the robustness of the ML feature layer.
* A ``SyntheticDataset`` builder that mixes clean and wash trades at a
  configurable ratio and returns a DataFrame compatible with
  ``detection.feature_engineering.FEATURE_NAMES``.

All output matches the schemas in ``ingestion.data_models`` (``Trade``,
``OrderBookEvent``, ``BridgeTransfer``) so records can be passed directly
into ``detection.feature_engineering.build_feature_vector``.

Reproducibility
---------------
Every profile accepts a ``seed`` parameter and uses
``numpy.random.default_rng(seed)`` exclusively — no calls to
``random.random()`` or ``np.random.seed()`` (global state).

Security notes
--------------
* Wallet addresses are generated via ``stellar_sdk.Keypair.random()``
  (cryptographic RNG) so they cannot collide with real wallets.
* EVM addresses for the cross-chain profile use a reserved test prefix
  (``0xDEAD``) to prevent accidental collision with real Ethereum addresses.
* Generated CSV files must not be committed to version control; add
  ``data/synthetic_*.csv`` to ``.gitignore``.
"""

from __future__ import annotations

import random
import string
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from ingestion.data_models import Asset, BridgeTransfer, OrderBookEvent, Trade

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

NATIVE = Asset(code="XLM", issuer=None)
USDC = Asset(code="USDC", issuer="GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")
BTC = Asset(code="BTC", issuer="GAUTUYY2THLF7SGITDFMXJVYH3LHDSMGEAKSBU267M2K7A3W543CKUEF")

# Round-lot amounts wash-trading bots commonly reuse (skew Benford distribution)
WASH_LOT_SIZES = (100.0, 200.0, 250.0, 500.0, 1000.0, 5000.0)

_B32 = string.ascii_uppercase + "234567"


# ---------------------------------------------------------------------------
# Address generators
# ---------------------------------------------------------------------------


def _stellar_address(rng: np.random.Generator) -> str:
    """Generate a valid Stellar G-address using the Stellar SDK (crypto RNG)."""
    try:
        from stellar_sdk import Keypair
        return Keypair.random().public_key
    except Exception:
        # Fallback for environments without stellar_sdk
        chars = list(_B32)
        rng.shuffle(chars)
        tail = "".join(rng.choice(list(_B32), size=55))
        return "G" + tail


def _evm_address(rng: np.random.Generator) -> str:
    """Generate a plausible EVM hex address with a reserved test prefix."""
    # 0xDEAD prefix marks synthetic/test addresses — never real Ethereum addrs
    hex_chars = "0123456789abcdef"
    body = "".join(rng.choice(list(hex_chars), size=36))
    return "0xDEAD" + body


# ---------------------------------------------------------------------------
# AttackProfileConfig
# ---------------------------------------------------------------------------


@dataclass
class AttackProfileConfig:
    """Configuration shared by all attack profiles.

    Attributes
    ----------
    n_wallets:
        Number of wallets in the wash-trading ring.
    n_trades:
        Target number of wash trades to generate.
    seed:
        Seed for ``numpy.random.default_rng``.  Use a time-based seed for
        production training runs to avoid overfitting to a fixed dataset.
    asset_pair:
        Primary asset pair string, used in ``OrderBookEvent.asset_pair``.
    evasion_noise_std:
        Standard deviation of multiplicative Gaussian noise added to trade
        amounts (as a fraction of the amount).  ``0.0`` = no evasion;
        ``0.5`` = moderate noise that shifts the Benford MAD downward.
    """

    n_wallets: int = 5
    n_trades: int = 200
    seed: int = 42
    asset_pair: str = "XLM/USDC"
    evasion_noise_std: float = 0.0


# ---------------------------------------------------------------------------
# BenfordEvasionMixin
# ---------------------------------------------------------------------------


class BenfordEvasionMixin:
    """Mixes configurable Gaussian noise into trade amounts.

    When ``config.evasion_noise_std > 0`` each amount is multiplied by a
    random factor drawn from ``N(1.0, evasion_noise_std)``, clipped to
    ``[0.5, 2.0]`` to keep amounts positive and realistic.  This perturbs
    the leading-digit distribution toward Benford's expected frequencies,
    testing the robustness of the Benford ML features against evasion.

    The mixin must be used alongside :class:`AttackProfile` (i.e. the
    concrete class must also inherit from ``AttackProfile``).
    """

    def _apply_evasion(
        self,
        amounts: list[float],
        rng: np.random.Generator,
        noise_std: float,
    ) -> list[float]:
        """Return amounts with multiplicative noise applied.

        Parameters
        ----------
        amounts:
            Raw trade amounts before evasion.
        rng:
            Seeded NumPy generator — ensures reproducibility.
        noise_std:
            Standard deviation of the multiplicative noise factor.
            Pass ``0.0`` to skip noise and return amounts unchanged.

        Returns
        -------
        list[float]
            Perturbed amounts, always positive (clipped to ``amount * 0.5``).
        """
        if noise_std <= 0.0:
            return amounts
        noise = rng.normal(1.0, noise_std, len(amounts))
        noise = np.clip(noise, 0.5, 2.0)
        return [max(1e-7, float(a) * float(n)) for a, n in zip(amounts, noise)]


# ---------------------------------------------------------------------------
# AttackProfile base class
# ---------------------------------------------------------------------------


class AttackProfile(ABC):
    """Abstract base for named wash-trading attack profiles.

    Each subclass simulates a distinct adversarial strategy.  All profiles:

    * Accept an :class:`AttackProfileConfig` controlling wallet count,
      trade count, seed, and optional Benford evasion noise.
    * Expose a :meth:`generate` method that returns synthetic records.
    * Use ``numpy.random.default_rng(config.seed)`` exclusively — no global
      random state is mutated.
    """

    def __init__(self, config: AttackProfileConfig) -> None:
        self.config = config
        self._rng = np.random.default_rng(config.seed)
        self._wallets: list[str] = [
            _stellar_address(self._rng) for _ in range(config.n_wallets)
        ]
        self._trade_counter = 0

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def generate(self) -> list[Trade] | tuple[list[Trade], list[Any]]:
        """Generate synthetic wash-trade records for this profile."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        self._trade_counter += 1
        return f"{self.__class__.__name__}_{self.config.seed}_{self._trade_counter}"

    def _make_trade(
        self,
        base_account: str,
        counter_account: str,
        amount: float,
        close_time: datetime,
        base_asset: Asset = NATIVE,
        counter_asset: Asset = USDC,
    ) -> Trade:
        price = float(self._rng.uniform(0.08, 0.15))
        return Trade(
            id=self._next_id(),
            ledger_close_time=close_time,
            base_account=base_account,
            counter_account=counter_account,
            base_asset=base_asset,
            counter_asset=counter_asset,
            base_amount=max(1e-7, amount),
            counter_amount=max(1e-7, round(amount * price, 7)),
            price=price,
            base_is_seller=bool(self._trade_counter % 2 == 0),
        )

    def _cluster_time(self, base: datetime, spread_seconds: float = 60.0) -> datetime:
        """Return a timestamp within *spread_seconds* of *base*."""
        offset = float(self._rng.uniform(0, spread_seconds))
        return base + timedelta(seconds=offset)

    def _poisson_times(self, n: int, lookback_days: int = 30) -> list[datetime]:
        """Return *n* timestamps following a Poisson process over *lookback_days*."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)
        total_seconds = lookback_days * 86400
        # Exponential inter-arrivals → Poisson process
        gaps = self._rng.exponential(scale=total_seconds / max(n, 1), size=n)
        offsets = np.cumsum(gaps)
        offsets = offsets / offsets[-1] * total_seconds if offsets[-1] > 0 else offsets
        return [start + timedelta(seconds=float(o)) for o in offsets]

    def _tight_cluster_times(self, n: int, spread_seconds: float = 120.0) -> list[datetime]:
        """Return *n* timestamps clustered tightly (gamma, low variance)."""
        now = datetime.now(timezone.utc)
        # Pick a random burst start in off-hours (00:00–05:59 UTC)
        base_offset = float(self._rng.uniform(0, 30 * 86400))
        base = now - timedelta(days=30) + timedelta(seconds=base_offset)
        base = base.replace(hour=int(self._rng.integers(0, 6)), minute=0, second=0, microsecond=0)
        # Gamma with shape=2 → tight cluster, low variance
        gaps = self._rng.gamma(shape=2.0, scale=spread_seconds / max(n, 1), size=n)
        offsets = np.cumsum(gaps)
        return [base + timedelta(seconds=float(o)) for o in offsets]


# ---------------------------------------------------------------------------
# RoundTripProfile
# ---------------------------------------------------------------------------


class RoundTripProfile(BenfordEvasionMixin, AttackProfile):
    """Circular A→B→C→A wash trades with matching amounts.

    This is the canonical wash-trading pattern: a ring of wallets passes
    the same asset around the ring repeatedly, generating artificial volume
    without any net change in holdings.

    The ring size is configurable (default 3).  Trade amounts are fixed
    round-lot sizes unless ``evasion_noise_std > 0``, in which case
    :class:`BenfordEvasionMixin` adds noise to make the digit distribution
    appear more organic.

    Features exercised
    ------------------
    - ``wash_ring_membership`` (graph feature)
    - ``round_trip_trade_frequency``
    - ``timing_tightness_score``
    - ``benford_*`` metrics (degraded when evasion is active)
    """

    def __init__(self, config: AttackProfileConfig, ring_size: int = 3) -> None:
        super().__init__(config)
        self.ring_size = min(ring_size, config.n_wallets)

    def generate(self) -> list[Trade]:
        """Return *n_trades* round-trip trades in a tight cluster."""
        ring = self._wallets[: self.ring_size]
        n = self.config.n_trades
        raw_amounts = [float(self._rng.choice(WASH_LOT_SIZES)) for _ in range(n)]
        amounts = self._apply_evasion(raw_amounts, self._rng, self.config.evasion_noise_std)
        times = self._tight_cluster_times(n)

        trades: list[Trade] = []
        for i in range(n):
            sender = ring[i % self.ring_size]
            receiver = ring[(i + 1) % self.ring_size]
            trades.append(self._make_trade(sender, receiver, amounts[i], times[i]))
        return trades


# ---------------------------------------------------------------------------
# LayeringProfile
# ---------------------------------------------------------------------------


class LayeringProfile(BenfordEvasionMixin, AttackProfile):
    """Layering: large order-book spoofs followed by genuine-looking buys.

    The attacker places a burst of large sell orders to push the apparent
    best-ask price down (creating false sell pressure), then cancels those
    orders and buys at the artificially depressed price.  The genuine buy
    trades are what appear in the ``Trade`` records; the cancelled sell
    orders appear as ``OrderBookEvent`` records with ``event_type="cancelled"``.

    Features exercised
    ------------------
    - ``order_cancellation_rate``
    - ``off_hours_activity_ratio``
    - ``volume_spike_frequency``
    - ``benford_*`` metrics
    """

    def generate(self) -> tuple[list[Trade], list[OrderBookEvent]]:  # type: ignore[override]
        """Return (wash_trades, order_book_events) for a layering campaign."""
        n = self.config.n_trades
        wallets = self._wallets
        times = self._tight_cluster_times(n, spread_seconds=300.0)

        trades: list[Trade] = []
        events: list[OrderBookEvent] = []
        raw_amounts = [float(self._rng.choice(WASH_LOT_SIZES)) * 10 for _ in range(n)]
        amounts = self._apply_evasion(raw_amounts, self._rng, self.config.evasion_noise_std)

        for i in range(n):
            attacker = wallets[i % len(wallets)]
            victim = wallets[(i + 1) % len(wallets)]
            t = times[i]

            # Phase 1: place + cancel a large sell order (spoofing the depth)
            spoof_amount = amounts[i]
            price = float(self._rng.uniform(0.08, 0.15))
            ev_id = f"LayerEvt_{self.config.seed}_{i}"
            events.append(OrderBookEvent(
                id=ev_id + "_place",
                timestamp=t,
                account=attacker,
                asset_pair=self.config.asset_pair,
                side="sell",
                amount=spoof_amount,
                price=price * 0.97,  # slightly below market to create pressure
                event_type="created",
            ))
            events.append(OrderBookEvent(
                id=ev_id + "_cancel",
                timestamp=t + timedelta(seconds=float(self._rng.uniform(5, 30))),
                account=attacker,
                asset_pair=self.config.asset_pair,
                side="sell",
                amount=spoof_amount,
                price=price * 0.97,
                event_type="cancelled",
            ))

            # Phase 2: genuine-looking buy at the manipulated price
            buy_amount = spoof_amount * float(self._rng.uniform(0.1, 0.3))
            buy_time = t + timedelta(seconds=float(self._rng.uniform(30, 90)))
            trades.append(self._make_trade(attacker, victim, buy_amount, buy_time))

        return trades, events
