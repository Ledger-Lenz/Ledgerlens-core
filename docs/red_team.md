# Red-Team Promotion Gate

Every model promotion runs the full adversarial red-team suite
(`detection/red_team/`) in CI and is blocked if it fails.

## How the gate works

`.github/workflows/model_promotion.yml` (on pushes to `main` touching
`detection/**` or `config/**`, or on manual dispatch):

1. **Train** a candidate model (`python cli.py train`).
2. **Red-team gate** — `python cli.py red-team` runs one campaign per
   `AttackType`. Each campaign evolves 20% of `--n-samples` wash-trade seeds
   with `GeneticAttacker` and measures the *evasion rate* (fraction scored below
   the detection threshold). The job fails, and promotion is skipped, if any
   campaign's evasion rate exceeds the threshold (default **5%**,
   `CAMPAIGN_EVASION_THRESHOLD` in `detection/red_team/runner.py`; override with
   the `evasion_threshold` dispatch input).
3. **Promote** — runs only when the gate job succeeds. It re-checks every
   model's current version with `model_registry.assert_red_team_passed` (a
   version with no recorded result is treated as failing) and publishes the
   models as the `promoted-models-<sha>` artifact.

`nightly_red_team.yml` continues to run the same suite nightly against a
freshly trained model for early warning.

## Results per model version

`cli.py red-team` records the campaign summary against each model's current
version as `models/<name>_v<version>.redteam.json` (disable with `--no-record`).
Use the registry to compare versions:

```python
from detection.model_registry import load_red_team_history

for r in load_red_team_history("random_forest", "models"):  # newest first
    print(r["version"], r["passed"], [c["evasion_rate"] for c in r["campaigns"]])
```

In CI, results accumulate in the cached `red_team_history/` directory; each
promotion run publishes a per-version comparison table to the job summary and
uploads it with the reports as the `red-team-<sha>` artifact.

## Adding a new red-team scenario

When a new evasion pattern is discovered:

1. Add a member to `AttackType` in `detection/red_team/runner.py`.
2. Add its mutation scale to `_ATTACK_MUTATION_SCALE` (how aggressively the
   attacker may perturb features; unlisted types default to `0.25`).
3. If the attack only touches specific features, restrict mutation via the
   `feature_constraints` built in the `red-team` command in `cli.py`
   (`{"min", "max", "mutable"}` per feature).
4. Run it locally against a trained model:
   `python cli.py red-team --model-dir models --n-samples 50 --no-record`.
5. Add a unit test for the new campaign alongside the existing red-team tests.

`run_all_campaigns` iterates every `AttackType`, so the new scenario is
automatically part of the promotion gate once merged.
