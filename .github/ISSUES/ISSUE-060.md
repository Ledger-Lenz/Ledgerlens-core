---
title: "Add Per-Round Differential Privacy Budget Accounting with Epsilon Exhaustion Guardrails"
labels: ["difficulty: advanced", "area: federated-learning", "type: feature"]
assignees: []
---

## Summary
The federated learning pipeline applies DP-SGD (ISSUE-070) to gradient updates but does not track cumulative privacy budget consumption across training rounds. Without epsilon accounting, it is impossible to guarantee the total privacy loss bound for a participating exchange's data. This issue implements per-client, per-round epsilon accounting using the Rényi Differential Privacy (RDP) accountant from Opacus, with automatic training suspension when the budget is exhausted.

## Background & Context
Differential privacy provides a mathematical guarantee: after all training rounds complete, the total privacy loss for any individual's data is bounded by ε. Each training round consumes a fraction of this budget. The RDP accountant tracks the cumulative ε across rounds, accounting for composition (multiple rounds increase total privacy loss).

Without accounting, the federated server could run indefinitely, consuming unbounded privacy budget. Regulators (GDPR Art. 25, CCPA) increasingly expect data controllers to quantify and bound privacy loss — epsilon accounting provides this quantification.

## Objectives
- [ ] Integrate Opacus `RDPAccountant` into `federated/fl_client.py`
- [ ] Track per-round (ε, δ) consumption and cumulative budget in the feature store `fl_privacy_budget` table
- [ ] Add `max_epsilon` configuration parameter; suspend training when cumulative ε exceeds threshold
- [ ] Expose budget status via `GET /federated/budget/{client_id}` endpoint
- [ ] Emit `privacy_budget_exhausted` webhook event when threshold is hit

## Technical Requirements
```python
# federated/fl_client.py
from opacus.accountants import RDPAccountant

class FLClient:
    def __init__(self, ..., max_epsilon: float = 10.0, delta: float = 1e-5):
        self.accountant = RDPAccountant()
        self.max_epsilon = max_epsilon
        self.delta = delta

    def train_round(self, noise_multiplier: float, sample_rate: float, steps: int):
        self.accountant.step(noise_multiplier=noise_multiplier, sample_rate=sample_rate)
        eps = self.accountant.get_epsilon(delta=self.delta)
        if eps > self.max_epsilon:
            raise PrivacyBudgetExhausted(f"ε={eps:.2f} exceeds max_epsilon={self.max_epsilon}")
        self._persist_budget(round_eps=eps)
        ...
```

Default `max_epsilon=10.0` with `delta=1e-5`. Configurable via env vars `FL_MAX_EPSILON` and `FL_DELTA`.

## Definition of Done
- [ ] Cumulative ε tracked and persisted after every round
- [ ] Training halts automatically when `max_epsilon` is exceeded
- [ ] Budget status endpoint returns current ε, rounds consumed, and remaining budget
- [ ] Tests verify budget exhaustion halts training and emits webhook

## For Contributors
Familiarity with differential privacy theory (RDP composition, (ε,δ)-DP) and the Opacus library required.
