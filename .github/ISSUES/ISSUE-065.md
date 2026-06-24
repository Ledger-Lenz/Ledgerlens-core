---
title: "Add Differential Privacy to Federated Learning Gradient Updates via DP-SGD and Opacus"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/federated/client.py` to add differentially private stochastic gradient descent (DP-SGD) using the Opacus library, ensuring that individual wallet transaction patterns cannot be recovered from federated model gradient updates. Implement per-sample gradient clipping (`clip_norm=1.0`), noise multiplier calibration for a target privacy budget of `(ε=1.0, δ=1e-5)`, and a privacy accountant that tracks cumulative ε across training rounds and halts training when the budget is exhausted.

## Background & Context

LedgerLens's federated learning architecture (Flower-based) allows multiple institutional clients — e.g., exchange compliance teams — to jointly train the wash-trade detection model without sharing raw wallet transaction data. However, federated learning alone does not provide formal privacy guarantees: gradient updates can leak information about individual training examples through gradient inversion attacks (e.g., DLG — Deep Leakage from Gradients).

Differential privacy (DP) provides a formal, quantifiable bound on this leakage. DP-SGD augments standard mini-batch SGD with two operations:
1. **Per-sample gradient clipping**: each individual sample's gradient is clipped to a maximum L2 norm (`clip_norm`), bounding the sensitivity of the aggregated gradient.
2. **Gaussian noise injection**: calibrated Gaussian noise is added to the clipped, summed gradient before the optimizer step. The noise magnitude is determined by the `noise_multiplier` parameter.

The **Rényi Differential Privacy (RDP) accountant** in Opacus tracks the cumulative privacy cost `(ε, δ)` across all gradient steps. When the target ε is reached, further training on sensitive data must stop. This ensures LedgerLens can make a binding privacy guarantee to participating clients.

The PyTorch model used in federated training must be the neural network variant of the LedgerLens classifier (or a wrapper around the sklearn/XGBoost models if a PyTorch-native model is introduced for FL). This issue should introduce a lightweight PyTorch MLP as the FL-specific model, separate from the primary RF/XGBoost/LightGBM ensemble used in non-FL scoring.

## Objectives

- [ ] Introduce `detection/federated/fl_model.py` defining a `WashTradeMLPClassifier` (PyTorch `nn.Module`) with configurable hidden layers, compatible with LedgerLens's 35-feature input schema.
- [ ] Extend `detection/federated/client.py` to wrap the model with `opacus.PrivacyEngine`, passing `max_grad_norm=1.0` and `noise_multiplier` (computed by `calibrate_noise_multiplier`).
- [ ] Implement `calibrate_noise_multiplier(target_epsilon, delta, sample_rate, epochs)` in `detection/federated/privacy_utils.py` using `opacus.accountants.utils.get_noise_multiplier`.
- [ ] Implement `PrivacyAccountant` class wrapping `opacus.accountants.RDPAccountant` with methods `step()`, `get_epsilon(delta)`, and `budget_exhausted(target_epsilon, delta) -> bool`.
- [ ] On each FL training round, call `PrivacyAccountant.step()` after each batch; after each round, log `ε` at INFO level.
- [ ] Halt FL training (return early, notify server) when `budget_exhausted()` returns True.
- [ ] Persist per-round privacy accounting to SQLite table `fl_privacy_log` (round, epsilon, delta, noise_multiplier, recorded_at).
- [ ] Expose `GET /admin/fl/privacy` endpoint returning the current privacy budget status.
- [ ] Configuration via `.env`: `FL_DP_TARGET_EPSILON=1.0`, `FL_DP_DELTA=1e-5`, `FL_DP_CLIP_NORM=1.0`.
- [ ] All new code covered by tests with ≥90% branch coverage.

## Technical Requirements

### `WashTradeMLPClassifier` (`detection/federated/fl_model.py`)

```python
import torch.nn as nn

class WashTradeMLPClassifier(nn.Module):
    def __init__(
        self,
        input_dim: int = 35,
        hidden_dims: list[int] = [128, 64, 32],
        dropout: float = 0.3,
    ):
        super().__init__()
        layers = []
        prev = input_dim
        for h in hidden_dims:
            layers += [nn.Linear(prev, h), nn.ReLU(), nn.Dropout(dropout)]
            prev = h
        layers.append(nn.Linear(prev, 1))   # binary classification
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x).squeeze(-1)      # logits
```

### `PrivacyAccountant` (`detection/federated/privacy_utils.py`)

```python
from opacus.accountants import RDPAccountant
from opacus.accountants.utils import get_noise_multiplier

class PrivacyAccountant:
    def __init__(self, noise_multiplier: float, sample_rate: float, delta: float):
        self._accountant = RDPAccountant()
        self.noise_multiplier = noise_multiplier
        self.sample_rate = sample_rate
        self.delta = delta

    def step(self, num_steps: int = 1) -> None:
        """Record num_steps of DP-SGD; call after each batch."""
        self._accountant.step(
            noise_multiplier=self.noise_multiplier,
            sample_rate=self.sample_rate,
            num_steps=num_steps,
        )

    def get_epsilon(self) -> float:
        return self._accountant.get_epsilon(self.delta)

    def budget_exhausted(self, target_epsilon: float) -> bool:
        return self.get_epsilon() >= target_epsilon

def calibrate_noise_multiplier(
    target_epsilon: float,
    delta: float,
    sample_rate: float,
    epochs: int,
    steps_per_epoch: int,
) -> float:
    return get_noise_multiplier(
        target_epsilon=target_epsilon,
        target_delta=delta,
        sample_rate=sample_rate,
        epochs=epochs,
        accountant="rdp",
    )
```

### DP-SGD training loop (`detection/federated/client.py`)

```python
from opacus import PrivacyEngine

def train_round(
    model: WashTradeMLPClassifier,
    dataloader: DataLoader,
    optimizer: torch.optim.Optimizer,
    privacy_engine: PrivacyEngine,
    accountant: PrivacyAccountant,
    target_epsilon: float,
) -> dict:
    """
    Run one FL training round with DP-SGD.
    Returns {"gradients": ..., "epsilon": float, "budget_exhausted": bool}
    """
    model.train()
    privacy_engine.attach(optimizer)      # Opacus modifies optimizer in-place
    for batch_x, batch_y in dataloader:
        if accountant.budget_exhausted(target_epsilon):
            logger.warning("Privacy budget exhausted. Halting training.")
            break
        optimizer.zero_grad()
        loss = F.binary_cross_entropy_with_logits(model(batch_x), batch_y.float())
        loss.backward()
        optimizer.step()
        accountant.step()
    return {
        "gradients": [p.grad.clone() for p in model.parameters()],
        "epsilon": accountant.get_epsilon(),
        "budget_exhausted": accountant.budget_exhausted(target_epsilon),
    }
```

### SQLite schema for privacy log

```sql
CREATE TABLE IF NOT EXISTS fl_privacy_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    epsilon         REAL NOT NULL,
    delta           REAL NOT NULL,
    noise_multiplier REAL NOT NULL,
    clip_norm       REAL NOT NULL,
    budget_exhausted INTEGER NOT NULL DEFAULT 0,
    recorded_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### API endpoint

```python
@router.get("/admin/fl/privacy", response_model=FLPrivacyStatus)
async def fl_privacy_status(...):
    """Return current cumulative epsilon, remaining budget, and per-round log."""
    ...

class FLPrivacyStatus(BaseModel):
    current_epsilon: float
    target_epsilon: float
    delta: float
    noise_multiplier: float
    clip_norm: float
    budget_exhausted: bool
    rounds_completed: int
```

### Configuration

```
FL_DP_TARGET_EPSILON=1.0
FL_DP_DELTA=1e-5
FL_DP_CLIP_NORM=1.0
FL_DP_SAMPLE_RATE=0.01    # fraction of dataset per batch (Poisson sampling)
FL_DP_EPOCHS_PER_ROUND=1  # local epochs per FL round
```

## Security Considerations

- **ε budget is a hard limit**: once `budget_exhausted()` is True, the client must refuse to participate in further training rounds involving sensitive wallet data. This should raise `PrivacyBudgetExhaustedError` rather than silently continuing.
- **Noise calibration must be reproducible**: `calibrate_noise_multiplier` inputs must be logged (not the noise_multiplier itself which would allow privacy reverse-engineering, but the parameters used) so auditors can verify the calibration.
- **Gradient transmission**: gradient updates are transmitted to the Flower server. They must not be logged at DEBUG level as they can be used to reconstruct training data. Log only gradient norms (scalars), not gradient tensors.
- **Model weights**: the FL model's weights represent aggregated privacy-protected gradients. Do not sign or persist them using the ED25519 model signing key used for the primary ensemble — use a separate FL model key to prevent cross-contamination.
- **`delta` must be << `1/n_training_samples`**: validate at calibration time that `delta < 1 / len(dataset)` and raise `ValueError` if not. A δ that is too large makes the DP guarantee trivially weak.

## Testing Requirements

- **Unit — `calibrate_noise_multiplier`**: assert output is a positive float; assert a higher `target_epsilon` yields a lower `noise_multiplier` (less noise needed for weaker privacy).
- **Unit — `PrivacyAccountant.step()`**: after N steps, `get_epsilon()` must return a positive, monotonically non-decreasing value.
- **Unit — `budget_exhausted()`**: mock accountant at ε=0.99 and ε=1.01; assert `False` and `True` respectively.
- **Unit — training halt**: inject a mock accountant that returns `budget_exhausted=True` on batch 3; assert training loop exits after batch 3 and `budget_exhausted` key is True in return dict.
- **Unit — delta validation**: assert `calibrate_noise_multiplier` raises `ValueError` when `delta >= 1 / n_samples`.
- **Unit — privacy log write**: after one training round, assert `fl_privacy_log` contains exactly one row with correct `round_number` and non-zero `epsilon`.
- **Integration — `GET /admin/fl/privacy`**: assert response contains `current_epsilon`, `budget_exhausted`, and `rounds_completed` matching the SQLite log.
- **Integration — budget exhausted behaviour**: simulate rounds until budget exhausted; assert subsequent round returns early with `budget_exhausted=True` in response.

## Documentation Requirements

- Docstrings on `PrivacyAccountant`, `calibrate_noise_multiplier`, and `train_round`.
- New file `docs/differential_privacy.md` covering: DP-SGD mechanics, ε/δ interpretation for LedgerLens operators, noise multiplier calibration procedure, and budget management guidance.
- Update `README.md` with FL differential privacy capability in the Features section.
- Document `FL_DP_*` environment variables in `.env.example`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `WashTradeMLPClassifier` implemented in `detection/federated/fl_model.py`.
- [ ] `PrivacyAccountant` and `calibrate_noise_multiplier` implemented in `detection/federated/privacy_utils.py`.
- [ ] `train_round()` in `client.py` applies DP-SGD via Opacus with per-sample clipping and noise injection.
- [ ] Budget exhaustion halts training and raises `PrivacyBudgetExhaustedError`.
- [ ] Privacy log persisted to `fl_privacy_log` SQLite table per round.
- [ ] `GET /admin/fl/privacy` endpoint live and admin-key gated.
- [ ] All unit and integration tests pass; ≥90% branch coverage.
- [ ] `docs/differential_privacy.md` written.
- [ ] `.env.example` and `CHANGELOG.md` updated.
- [ ] Gradient tensors never appear in log output.

## For Contributors

**Ideal contributor profile**: You have hands-on experience with differential privacy in ML training — specifically Opacus, DP-SGD, and the RDP accountant. You understand the relationship between `noise_multiplier`, `clip_norm`, `sample_rate`, and the resulting `(ε, δ)` guarantee. Familiarity with Flower (federated learning framework) and PyTorch is required. Experience designing privacy budgets for fraud detection or financial ML workloads is a significant advantage.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., differential privacy, federated learning, PyTorch ML pipelines).
2. **Relevant experience**: Opacus or DP-SGD implementations you have shipped; any published work on privacy-preserving ML.
3. **Approach / thoughts**: your view on the ε=1.0 target — is it appropriate for fraud detection on-chain data? What are the utility/privacy tradeoffs at ε=1.0 vs ε=0.1?
4. **Estimated time**: realistic estimate to complete implementation, tests, and documentation to the Definition of Done standard.
