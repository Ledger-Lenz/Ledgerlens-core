"""Continuous runner that drives the red team loop against the live model.

Loads seed wash-trade feature vectors, attacks the current model with the
:class:`~detection.red_team.attacker.GeneticAttacker`, logs successful evasions,
and periodically evaluates the automated hardening trigger.  Designed to run on a
background thread so it never blocks inference.

Also provides :class:`RedTeamRunner` for running structured campaign-based
adversarial evaluations (Issue-133).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import numpy as np

from detection.red_team import EVASION_THRESHOLD, N_EVASION_TRIGGER
from detection.red_team.attacker import GeneticAttacker, evaluate_score
from detection.red_team.evasion_logger import log_evasion, maybe_trigger_hardening

logger = logging.getLogger("ledgerlens.red_team.runner")

CAMPAIGN_EVASION_THRESHOLD = 0.05  # 5% evasion rate gate


class AttackType(str, Enum):
    AMOUNT_CAMOUFLAGE = "amount_camouflage"
    TIMING_JITTER = "timing_jitter"
    GRAPH_FRAGMENTATION = "graph_fragmentation"
    SYBIL_CHAIN = "sybil_chain"
    BENFORD_MIMICRY = "benford_mimicry"


# Per-attack mutation overrides applied on top of the base feature constraints.
_ATTACK_MUTATION_SCALE: dict[AttackType, float] = {
    AttackType.AMOUNT_CAMOUFLAGE: 0.30,
    AttackType.TIMING_JITTER: 0.20,
    AttackType.GRAPH_FRAGMENTATION: 0.40,
    AttackType.SYBIL_CHAIN: 0.35,
    AttackType.BENFORD_MIMICRY: 0.25,
}


@dataclass
class RedTeamCampaign:
    attack_type: AttackType
    n_samples: int
    n_evaded: int
    evasion_rate: float
    threshold: float
    passed: bool
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    params: dict = field(default_factory=dict)


@dataclass
class CampaignSummary:
    campaigns: list[RedTeamCampaign] = field(default_factory=list)
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.campaigns)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "executed_at": self.executed_at.isoformat(),
            "campaigns": [
                {
                    "attack_type": c.attack_type.value,
                    "n_samples": c.n_samples,
                    "n_evaded": c.n_evaded,
                    "evasion_rate": c.evasion_rate,
                    "threshold": c.threshold,
                    "passed": c.passed,
                    "executed_at": c.executed_at.isoformat(),
                    "params": c.params,
                }
                for c in self.campaigns
            ],
        }


class RedTeamRunner:
    """Orchestrates adversarial campaigns across all registered attack types.

    Each campaign generates a synthetic wash-trade dataset, applies one attack
    type to 20% of the positive samples, scores with the current model, and
    computes the evasion rate (fraction of attacked samples that fall below the
    detection threshold).  A :class:`CampaignSummary` is returned with a
    ``passed`` flag — False if any attack exceeds ``evasion_threshold``.
    """

    def __init__(
        self,
        model,
        feature_constraints: dict,
        evasion_threshold: float = CAMPAIGN_EVASION_THRESHOLD,
        detection_threshold: float = EVASION_THRESHOLD,
        n_generations: int = 50,
        seed: Optional[int] = None,
        report_dir: str = "./red_team_reports",
    ) -> None:
        self.model = model
        self.feature_constraints = feature_constraints
        self.evasion_threshold = evasion_threshold
        self.detection_threshold = detection_threshold
        self.n_generations = n_generations
        self.rng = np.random.default_rng(seed)
        self.report_dir = report_dir
        self._attack_types = list(AttackType)

    def _build_seed_vectors(self, n: int) -> list[np.ndarray]:
        """Build synthetic wash-trade feature vectors for seeding attacks."""
        list(self.feature_constraints.keys())
        seeds = []
        for _ in range(n):
            vec = np.array(
                [
                    float(self.rng.uniform(c.get("min", 0.0), c.get("max", 1.0)))
                    if c.get("mutable", True)
                    else float(c.get("min", 0.0))
                    for c in self.feature_constraints.values()
                ],
                dtype=float,
            )
            seeds.append(vec)
        return seeds

    def run_campaign(
        self,
        attack_type: AttackType,
        n_samples: int = 100,
        threshold: Optional[float] = None,
    ) -> RedTeamCampaign:
        """Run a single campaign for ``attack_type``.

        Applies the attack to 20% of ``n_samples`` positive (wash) seeds,
        scores each attacked vector, and returns a :class:`RedTeamCampaign`.
        """
        threshold = threshold if threshold is not None else self.evasion_threshold
        mutation_scale = _ATTACK_MUTATION_SCALE.get(attack_type, 0.25)
        n_attacked = max(1, int(n_samples * 0.20))

        seeds = self._build_seed_vectors(n_attacked)
        n_evaded = 0

        for seed_vec in seeds:
            attacker = GeneticAttacker(
                self.model,
                self.feature_constraints,
                mutation_scale=mutation_scale,
                seed=int(self.rng.integers(0, 2**31 - 1)),
            )
            _, score = attacker.evolve(seed_vec, n_generations=self.n_generations)
            if score < self.detection_threshold:
                n_evaded += 1

        evasion_rate = n_evaded / n_attacked if n_attacked > 0 else 0.0
        return RedTeamCampaign(
            attack_type=attack_type,
            n_samples=n_attacked,
            n_evaded=n_evaded,
            evasion_rate=evasion_rate,
            threshold=threshold,
            passed=evasion_rate <= threshold,
            params={"mutation_scale": mutation_scale, "n_generations": self.n_generations},
        )

    def run_all_campaigns(self, n_samples: int = 100) -> CampaignSummary:
        """Run campaigns for every registered attack type and return a :class:`CampaignSummary`."""
        summary = CampaignSummary()
        for attack_type in self._attack_types:
            campaign = self.run_campaign(attack_type, n_samples=n_samples)
            summary.campaigns.append(campaign)
            level = logging.WARNING if not campaign.passed else logging.INFO
            logger.log(
                level,
                "Campaign %s: evasion_rate=%.3f passed=%s",
                attack_type.value,
                campaign.evasion_rate,
                campaign.passed,
            )
        return summary

    def write_report(self, summary: CampaignSummary) -> str:
        """Persist ``summary`` to ``report_dir/YYYYMMDD_HHMM.json`` and return the path."""
        os.makedirs(self.report_dir, exist_ok=True)
        ts = summary.executed_at.strftime("%Y%m%d_%H%M")
        path = os.path.join(self.report_dir, f"{ts}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary.to_dict(), fh, indent=2)
        logger.info("Wrote campaign report to %s", path)
        return path


def load_random_seeds(seed_dataset_path: str, n: int, rng=None) -> list[dict]:
    """Load up to ``n`` random seed feature dicts from a JSON dataset.

    The dataset is a JSON file containing a list of ``{feature: value}`` objects
    (feature vectors of known wash trades).  Fewer than ``n`` rows are returned
    when the dataset is smaller.
    """
    rng = rng if rng is not None else np.random.default_rng()
    with open(seed_dataset_path, "r", encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        raise ValueError("seed dataset must be a JSON list of feature objects")
    if not rows:
        return []
    n = min(n, len(rows))
    idx = rng.choice(len(rows), size=n, replace=False)
    return [rows[int(i)] for i in idx]


def run_red_team_loop(
    model,
    seed_dataset_path: str,
    feature_constraints: dict,
    poll_interval_seconds: int = 300,
    n_seeds_per_round: int = 20,
    n_generations: int = 100,
    threshold: float = EVASION_THRESHOLD,
    n_trigger: int = N_EVASION_TRIGGER,
    retrain_callback=None,
    stop_event: threading.Event | None = None,
    max_iterations: int | None = None,
    db_path: str | None = None,
    seed: int | None = None,
) -> int:
    """Continuously attack the current model and log evasions.

    Each round samples ``n_seeds_per_round`` seeds, evolves an attack per seed,
    logs any evasion that beats ``threshold``, then checks the hardening trigger.

    The loop terminates when ``stop_event`` is set or after ``max_iterations``
    rounds (whichever comes first); leave both unset for a truly continuous loop.
    Returns the number of rounds executed.

    Pacing uses ``stop_event.wait(poll_interval_seconds)`` rather than a bare
    sleep, so a background loop can be cancelled promptly without blocking.
    """
    rng = np.random.default_rng(seed)
    feature_names = list(feature_constraints.keys())
    rounds = 0

    while not (stop_event is not None and stop_event.is_set()):
        seeds = load_random_seeds(seed_dataset_path, n_seeds_per_round, rng)
        for seed_features in seeds:
            seed_array = np.array([seed_features.get(f, 0.0) for f in feature_names], dtype=float)
            attacker = GeneticAttacker(
                model, feature_constraints, seed=int(rng.integers(0, 2**31 - 1))
            )
            best, score = attacker.evolve(seed_array, n_generations=n_generations)
            if score < threshold:
                log_evasion(
                    original_features=seed_features,
                    evasion_features=attacker.to_dict(best),
                    original_score=evaluate_score(model, seed_features),
                    evasion_score=score,
                    attacker_generation=getattr(attacker, "last_generation", n_generations),
                    threshold=threshold,
                    db_path=db_path,
                )

        maybe_trigger_hardening(
            n_trigger=n_trigger,
            threshold=threshold,
            retrain_callback=retrain_callback,
            db_path=db_path,
        )

        rounds += 1
        if max_iterations is not None and rounds >= max_iterations:
            break
        if stop_event is not None:
            if stop_event.wait(poll_interval_seconds):
                break
        else:  # pragma: no cover - only hit by a genuinely unbounded loop
            import time

            time.sleep(poll_interval_seconds)

    return rounds


def start_red_team_loop(*args, **kwargs) -> threading.Thread:
    """Start :func:`run_red_team_loop` on a daemon thread and return it.

    Accepts the same arguments as :func:`run_red_team_loop`.  The thread is a
    daemon so it never keeps the process alive, satisfying the requirement that
    the red team loop run in the background without blocking inference.
    """
    thread = threading.Thread(
        target=run_red_team_loop, args=args, kwargs=kwargs, name="red-team-loop", daemon=True
    )
    thread.start()
    return thread
