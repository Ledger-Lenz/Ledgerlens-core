"""Tests for Bayesian cross-chain link scoring in detection/cross_chain_linker.py."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from detection.cross_chain_linker import (
    AMOUNT_TOLERANCE,
    CONFIDENCE_THRESHOLD,
    CONFIRMED_THRESHOLD,
    CrossChainLinker,
    LinkStatus,
    WalletLinkHypothesis,
    _validate_evm_address,
)
from ingestion.data_models import BridgeTransfer

STELLAR = "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF"
EVM = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _bridge(
    direction: str = "stellar_to_evm",
    ts: datetime | None = None,
    amount: float = 100.0,
) -> BridgeTransfer:
    return BridgeTransfer(
        chain="ethereum",
        direction=direction,
        evm_wallet=EVM,
        stellar_wallet=STELLAR,
        amount_usd=amount,
        token="USDC",
        tx_hash_evm="0x" + "aa" * 32,
        timestamp=ts or NOW,
    )


# ── _timing_likelihood ──────────────────────────────────────────────────


class TestTimingLikelihood:
    def setup_method(self):
        self.linker = CrossChainLinker()

    def test_zero_delta_yields_maximum(self):
        lr = self.linker._timing_likelihood(0.0)
        assert lr > 3000  # Gaussian(0) / Uniform is very high

    def test_two_sigma_yields_lower(self):
        lr_0 = self.linker._timing_likelihood(0.0)
        lr_600 = self.linker._timing_likelihood(600.0)
        assert lr_600 > 0
        assert lr_600 < lr_0

    def test_one_hour_yields_near_zero(self):
        lr = self.linker._timing_likelihood(3600.0)
        assert lr < 1.0


# ── _amount_likelihood ───────────────────────────────────────────────────


class TestAmountLikelihood:
    def setup_method(self):
        self.linker = CrossChainLinker()

    def test_within_tolerance_returns_strong(self):
        # 0.4% difference
        lr = self.linker._amount_likelihood(100.0, 100.4)
        assert lr == 10.0

    def test_boundary_inclusive(self):
        # Exactly 0.5% difference
        lr = self.linker._amount_likelihood(100.0, 100.5)
        assert lr == 10.0

    def test_beyond_tolerance_returns_neutral(self):
        # 0.51% difference
        lr = self.linker._amount_likelihood(100.0, 100.51)
        assert lr == 1.0

    def test_zero_stellar_amount(self):
        lr = self.linker._amount_likelihood(0.0, 100.0)
        assert lr == 1.0


# ── score_hypothesis ─────────────────────────────────────────────────────


class TestScoreHypothesis:
    def setup_method(self):
        self.linker = CrossChainLinker()

    def test_no_events_returns_rejected(self):
        h = self.linker.score_hypothesis(STELLAR, EVM, [])
        assert h.confidence == 0.0
        assert h.link_status == LinkStatus.REJECTED
        assert h.bridge_event_count == 0

    def test_strong_evidence_confirmed(self):
        events = [
            _bridge("stellar_to_evm", ts=NOW, amount=100.0),
            _bridge("evm_to_stellar", ts=NOW + timedelta(seconds=5), amount=100.2),
        ]
        h = self.linker.score_hypothesis(STELLAR, EVM, events)
        assert h.confidence > 0.9
        assert h.link_status == LinkStatus.CONFIRMED

    def test_weak_evidence_rejected(self):
        events = [
            _bridge("stellar_to_evm", ts=NOW, amount=100.0),
            _bridge("stellar_to_evm", ts=NOW + timedelta(hours=2), amount=500.0),
        ]
        h = self.linker.score_hypothesis(STELLAR, EVM, events)
        assert h.confidence < CONFIDENCE_THRESHOLD
        assert h.link_status == LinkStatus.REJECTED

    def test_invalid_evm_address_raises(self):
        with pytest.raises(ValueError, match="Malformed EVM address"):
            self.linker.score_hypothesis(STELLAR, "not-an-address", [])

    def test_confidence_clamped_to_unit_interval(self):
        events = [
            _bridge("stellar_to_evm", ts=NOW, amount=100.0),
            _bridge("evm_to_stellar", ts=NOW, amount=100.0),
        ]
        h = self.linker.score_hypothesis(STELLAR, EVM, events)
        assert 0.0 <= h.confidence <= 1.0


# ── _direction_consistency_likelihood ────────────────────────────────────


class TestDirectionConsistency:
    def setup_method(self):
        self.linker = CrossChainLinker()

    def test_alternating_directions_high_score(self):
        events = [
            _bridge("stellar_to_evm", ts=NOW),
            _bridge("evm_to_stellar", ts=NOW + timedelta(hours=1)),
            _bridge("stellar_to_evm", ts=NOW + timedelta(hours=2)),
        ]
        lr = self.linker._direction_consistency_likelihood(events)
        assert lr == pytest.approx(5.0)

    def test_same_direction_neutral(self):
        events = [
            _bridge("stellar_to_evm", ts=NOW),
            _bridge("stellar_to_evm", ts=NOW + timedelta(hours=1)),
        ]
        lr = self.linker._direction_consistency_likelihood(events)
        assert lr == pytest.approx(1.0)

    def test_single_event_neutral(self):
        lr = self.linker._direction_consistency_likelihood([_bridge()])
        assert lr == 1.0


# ── persist_hypothesis ───────────────────────────────────────────────────


class TestPersistHypothesis:
    def test_rejected_not_persisted(self, tmp_path):
        db = str(tmp_path / "test.db")
        linker = CrossChainLinker(db_path=db)
        h = WalletLinkHypothesis(
            stellar_wallet=STELLAR, evm_wallet=EVM,
            evidence_features={}, log_likelihood_ratio=-10.0,
            confidence=0.3, link_status=LinkStatus.REJECTED,
            bridge_event_count=0,
        )
        linker.persist_hypothesis(h)
        assert linker.get_accepted_links(STELLAR) == []

    def test_upsert_updates_confidence(self, tmp_path):
        db = str(tmp_path / "test.db")
        linker = CrossChainLinker(db_path=db)

        h1 = WalletLinkHypothesis(
            stellar_wallet=STELLAR, evm_wallet=EVM,
            evidence_features={"timing_similarity": 5.0},
            log_likelihood_ratio=2.0, confidence=0.75,
            link_status=LinkStatus.PROBABLE, bridge_event_count=2,
        )
        linker.persist_hypothesis(h1)

        h2 = WalletLinkHypothesis(
            stellar_wallet=STELLAR, evm_wallet=EVM,
            evidence_features={"timing_similarity": 10.0},
            log_likelihood_ratio=4.0, confidence=0.95,
            link_status=LinkStatus.CONFIRMED, bridge_event_count=4,
        )
        linker.persist_hypothesis(h2)

        links = linker.get_accepted_links(STELLAR)
        assert len(links) == 1
        assert links[0].confidence == pytest.approx(0.95)

    def test_get_accepted_links_sorted_by_confidence(self, tmp_path):
        db = str(tmp_path / "test.db")
        linker = CrossChainLinker(db_path=db)
        evm2 = "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD"

        h1 = WalletLinkHypothesis(
            stellar_wallet=STELLAR, evm_wallet=EVM,
            evidence_features={}, log_likelihood_ratio=2.0,
            confidence=0.75, link_status=LinkStatus.PROBABLE,
            bridge_event_count=2,
        )
        h2 = WalletLinkHypothesis(
            stellar_wallet=STELLAR, evm_wallet=evm2,
            evidence_features={}, log_likelihood_ratio=4.0,
            confidence=0.92, link_status=LinkStatus.CONFIRMED,
            bridge_event_count=4,
        )
        linker.persist_hypothesis(h1)
        linker.persist_hypothesis(h2)

        links = linker.get_accepted_links(STELLAR)
        assert len(links) == 2
        assert links[0].confidence > links[1].confidence

    def test_min_confidence_filter(self, tmp_path):
        db = str(tmp_path / "test.db")
        linker = CrossChainLinker(db_path=db)
        evm2 = "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD"

        h1 = WalletLinkHypothesis(
            stellar_wallet=STELLAR, evm_wallet=EVM,
            evidence_features={}, log_likelihood_ratio=2.0,
            confidence=0.75, link_status=LinkStatus.PROBABLE,
            bridge_event_count=2,
        )
        h2 = WalletLinkHypothesis(
            stellar_wallet=STELLAR, evm_wallet=evm2,
            evidence_features={}, log_likelihood_ratio=4.0,
            confidence=0.92, link_status=LinkStatus.CONFIRMED,
            bridge_event_count=4,
        )
        linker.persist_hypothesis(h1)
        linker.persist_hypothesis(h2)

        links = linker.get_accepted_links(STELLAR, min_confidence=0.9)
        assert len(links) == 1
        assert links[0].confidence >= 0.9


# ── _sigmoid edge cases ─────────────────────────────────────────────────


class TestSigmoid:
    def setup_method(self):
        self.linker = CrossChainLinker()

    def test_large_positive(self):
        assert self.linker._sigmoid(1000) == 1.0

    def test_large_negative(self):
        assert self.linker._sigmoid(-1000) == 0.0

    def test_zero(self):
        assert self.linker._sigmoid(0.0) == pytest.approx(0.5)
