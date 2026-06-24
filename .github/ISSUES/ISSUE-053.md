---
title: "Automate Red-Team Attack Campaigns with Nightly CI Integration"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/red_team/runner.py` to run automated adversarial attack campaigns against the live model nightly in CI. For each attack type (amount camouflage, timing jitter, graph fragmentation), measure evasion rate and alert if any attack achieves > 5% evasion rate against the current production model. This creates a continuous adversarial robustness gate that catches model regressions before they reach production.

## Background & Context

`detection/red_team/attacker.py` already implements individual attack primitives. `detection/red_team/evasion_logger.py` logs individual attack outcomes. What is missing is an orchestration layer that:

1. Generates a full synthetic wash-trade dataset with known ground truth
2. Applies each attack type to 20% of the positive (wash) samples
3. Scores the attacked samples with the current production model
4. Measures evasion rate: fraction of attacked wash wallets that fall below the detection threshold
5. Writes a machine-readable evasion report
6. Exits with code 1 if any attack type exceeds the 5% evasion threshold (CI gate)

The nightly CI job (GitHub Actions) runs this after every model retrain and on a fixed schedule. If the evasion gate trips, the CI workflow sends a Slack/webhook alert and blocks promotion of the new model.

## Objectives

- [ ] Implement `RedTeamCampaign` dataclass grouping attack type, attack parameters, evasion rate, and sample counts
- [ ] Implement `RedTeamRunner.run_campaign(attack_type, n_samples, threshold)` that applies attack, scores, and returns a `RedTeamCampaign`
- [ ] Implement `RedTeamRunner.run_all_campaigns()` executing all registered attack types and returning a `CampaignSummary`
- [ ] Implement `CampaignSummary.passed` property returning `True` iff all evasion rates ≤ 5%
- [ ] Write a `CampaignReport` to `./red_team_reports/YYYYMMDD_HHMM.json`
- [ ] Add `cli.py red-team` command that exits with code 0 (all passed) or 1 (any failed)
- [ ] Add `.github/workflows/nightly_red_team.yml` CI workflow running the campaign nightly at 03:00 UTC
- [ ] Expose `GET /admin/red-team/latest` returning the most recent `CampaignSummary`
- [ ] Write unit tests for each attack type's evasion rate measurement logic

## Technical Requirements

### Data structures

```python
# detection/red_team/runner.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

class AttackType(str, Enum):
    AMOUNT_CAMOUFLAGE   = "amount_camouflage"
    TIMING_JITTER       = "timing_jitter"
    GRAPH_FRAGMENTATION = "graph_fragmentation"
    SYBIL_CHAIN         = "sybil_chain"
    BENFORD_MIMICRY     = "benford_mimicry"

@dataclass
class RedTeamCampaign:
    attack_type: AttackType
    n_attacked: int
    n_evaded: int
    evasion_rate: float          # n_evaded / n_attacked
    detection_threshold: int     # score below this = evaded (default: 50)
    mean_score_attacked: float   # mean risk score of attacked samples
    mean_score_clean: float      # mean risk score of unattacked positives
    passed: bool                 # evasion_rate <= max_evasion_rate
    attack_params: dict          # parameters used for this campaign
    run_at: datetime = field(default_factory=datetime.utcnow)

@dataclass
class CampaignSummary:
    campaigns: list[RedTeamCampaign]
    model_version: str
    overall_passed: bool
    max_evasion_rate: float
    worst_attack: Optional[str]
    run_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def passed(self) -> bool:
        return self.overall_passed
```

### Runner implementation

```python
class RedTeamRunner:
    def __init__(
        self,
        inference_engine,           # ModelInferenceEngine
        synthetic_data_generator,   # from ingestion/synthetic_data.py
        max_evasion_rate: float = 0.05,
        detection_threshold: int = 50,
        n_samples_per_attack: int = 500,
        random_seed: int = 42,
    ): ...

    def run_campaign(
        self,
        attack_type: AttackType,
        attack_params: Optional[dict] = None,
    ) -> RedTeamCampaign:
        """
        1. Generate n_samples_per_attack labelled wash-trade feature vectors.
        2. Apply attacker.attack(features, attack_type, params) to each.
        3. Score attacked features with inference_engine.
        4. Count evasions (score < detection_threshold).
        5. Return RedTeamCampaign.
        """
        rng = np.random.default_rng(self.random_seed)
        positives = self._synth_gen.generate_wash_features(self.n_samples_per_attack, rng)
        attacked = [self._attacker.attack(fv, attack_type, attack_params) for fv in positives]
        scores_attacked = [self._engine.score_features(fv) for fv in attacked]
        scores_clean = [self._engine.score_features(fv) for fv in positives]
        n_evaded = sum(s < self.detection_threshold for s in scores_attacked)
        return RedTeamCampaign(
            attack_type=attack_type,
            n_attacked=len(attacked),
            n_evaded=n_evaded,
            evasion_rate=n_evaded / len(attacked),
            detection_threshold=self.detection_threshold,
            mean_score_attacked=float(np.mean(scores_attacked)),
            mean_score_clean=float(np.mean(scores_clean)),
            passed=n_evaded / len(attacked) <= self.max_evasion_rate,
            attack_params=attack_params or {},
        )

    def run_all_campaigns(self) -> CampaignSummary:
        campaigns = [self.run_campaign(at) for at in AttackType]
        worst = max(campaigns, key=lambda c: c.evasion_rate)
        return CampaignSummary(
            campaigns=campaigns,
            model_version=self._engine.model_version,
            overall_passed=all(c.passed for c in campaigns),
            max_evasion_rate=worst.evasion_rate,
            worst_attack=worst.attack_type.value if not worst.passed else None,
        )
```

### Attack implementations (in `attacker.py`)

```python
class Attacker:
    def attack(
        self,
        feature_vector: dict[str, float],
        attack_type: AttackType,
        params: Optional[dict] = None,
    ) -> dict[str, float]:
        ...

    def _amount_camouflage(self, fv: dict, params: dict) -> dict:
        """
        Reduce chi_sq_* and mad_* features by applying Benford noise.
        Target: reduce chi_sq_24h by 60%, mad_24h by 70%.
        """
        ...

    def _timing_jitter(self, fv: dict, params: dict) -> dict:
        """
        Increase iat_variance by adding Gaussian jitter (sigma=2.0s).
        Reduce temporal_anomaly_score by 0.4 (simulating jittered timing).
        """
        ...

    def _graph_fragmentation(self, fv: dict, params: dict) -> dict:
        """
        Set wash_ring_membership=0, wash_ring_size=0 (ring broken into sub-threshold SCCs).
        Reduce network_centrality by 80%.
        """
        ...
```

### CI workflow

```yaml
# .github/workflows/nightly_red_team.yml
name: Nightly Red Team Campaign

on:
  schedule:
    - cron: "0 3 * * *"   # 03:00 UTC daily
  workflow_dispatch:

jobs:
  red-team:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run red team campaign
        run: python cli.py red-team --exit-on-failure
      - name: Upload report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: red-team-report
          path: ./red_team_reports/
      - name: Notify on failure
        if: failure()
        run: |
          curl -s -X POST "${{ secrets.LEDGERLENS_WEBHOOK_URL }}" \
            -H "Content-Type: application/json" \
            -d '{"event": "red_team_failure", "repo": "${{ github.repository }}"}'
```

### SQLite persistence

```sql
CREATE TABLE IF NOT EXISTS red_team_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version TEXT NOT NULL,
    overall_passed BOOLEAN NOT NULL,
    max_evasion_rate REAL NOT NULL,
    worst_attack  TEXT,
    report_json   TEXT NOT NULL,
    run_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### Configuration

```
RED_TEAM_MAX_EVASION_RATE=0.05
RED_TEAM_DETECTION_THRESHOLD=50
RED_TEAM_N_SAMPLES=500
RED_TEAM_RANDOM_SEED=42
```

## Security Considerations

- **Report confidentiality**: `CampaignReport` JSON files contain model evasion rates and attack effectiveness. Do not commit these to the repository. Add `red_team_reports/` to `.gitignore`
- **CI secret isolation**: the `LEDGERLENS_WEBHOOK_URL` in the CI workflow is a GitHub secret. Ensure it is not echoed in workflow logs (`echo $SECRET` must never appear)
- **Attack reproducibility vs operational security**: `random_seed=42` makes campaigns reproducible for debugging but means a public adversary who knows the seed can exactly replicate the attack. Document that the seed should be rotated quarterly and is not a security parameter
- **Attack params exposure**: attack parameters are logged in `CampaignReport`. Avoid logging parameters that reveal internal thresholds (e.g., the exact `min_cycle_volume` used in graph_engine). Log parameter names but mask values above a configurable sensitivity level
- **Model version pinning**: the red-team campaign must always run against the model version returned by `models/random_forest_latest.txt`. Never hardcode a version string — this prevents comparing against a stale model

## Testing Requirements

- [ ] `tests/test_red_team_runner.py` — unit tests for runner and attack implementations
- [ ] Test: amount_camouflage attack reduces mean `chi_sq_24h` by at least 40% in the attacked vectors
- [ ] Test: timing_jitter increases `iat_variance` in attacked vectors
- [ ] Test: graph_fragmentation sets `wash_ring_membership = 0` in attacked vectors
- [ ] Test: `RedTeamRunner.run_campaign` returns `passed=True` for a "no-op" attack (identity transform)
- [ ] Test: `CampaignSummary.passed` is `False` if any campaign has evasion_rate > max_evasion_rate
- [ ] Test: `run_all_campaigns` runs all 5 attack types and returns correct `worst_attack`
- [ ] Test: `cli.py red-team` exits with code 0 when all campaigns pass; code 1 when any fail
- [ ] Test: report JSON is written to `./red_team_reports/` with correct schema

## Documentation Requirements

- [ ] Docstrings on `RedTeamRunner`, `RedTeamCampaign`, `CampaignSummary`, `Attacker` methods
- [ ] Update `docs/adversarial_robustness.md` with the automated campaign framework, how to interpret evasion rates, and what to do when a campaign fails
- [ ] Add `docs/red_team_runbook.md` with instructions for: interpreting CI failures, running campaigns locally, adding new attack types, rotating the random seed
- [ ] Document the `red_team_runs` SQLite table in `docs/database_schema.md`
- [ ] Update `.env.example` with four new configuration variables

## Definition of Done

- [ ] `RedTeamRunner`, `RedTeamCampaign`, `CampaignSummary` implemented
- [ ] All 5 attack types implemented in `Attacker`
- [ ] `cli.py red-team --exit-on-failure` exits with correct code
- [ ] `.github/workflows/nightly_red_team.yml` created
- [ ] `GET /admin/red-team/latest` endpoint live
- [ ] All unit tests pass
- [ ] `red_team_reports/` in `.gitignore`
- [ ] `docs/red_team_runbook.md` authored

## For Contributors

**Ideal contributor profile**: You have experience in adversarial machine learning red-teaming — either in academia (evasion attack papers) or in industry (model security audits, bug bounty ML programs). You are comfortable implementing both the attack side (perturbing feature vectors) and the measurement framework (evasion rate calculation, CI integration). Experience with GitHub Actions and Python CLI tools (Typer or Click) is expected.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "adversarial ML red-teaming", "model security auditing", "CI/CD for ML systems"
2. **Relevant experience** — adversarial attack campaigns you have run; CI pipelines for ML security gates; any published evasion attack research
3. **Approach / initial thoughts** — your thoughts on the 5% evasion threshold; additional attack types you would add beyond the five proposed; concerns about using fixed random seeds in security-sensitive contexts
4. **Estimated time** — breakdown by component (attacker, runner, report, CI workflow, API, tests, docs)
