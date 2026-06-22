# Federated Learning in LedgerLens

## Overview

LedgerLens supports a privacy-preserving Federated Learning (FL) mode that allows exchange operators (wallets, custodians, DEX aggregators) to improve the global wash-trading detection model using their private labelled datasets **without sharing raw transaction data**.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                      Exchange Operator (N nodes)                       │
│                                                                        │
│  Private Labelled Data                                                 │
│  (transactions + ground-  ──► Local RF/XGB/LGBM ──► Soft Labels p_i  │
│   truth compliance labels)    Ensemble Training      on X_pub         │
│                                                           │            │
│                    Ed25519-signed update: (p_i, n_i)      │            │
└──────────────────────────────────────────────────────────┼────────────┘
                                                           │ HTTPS
                                                           ▼
┌────────────────────────────────────────────────────────────────────────┐
│                    Federated Aggregation Server                        │
│                                                                        │
│  1. Verify Ed25519 signature                                           │
│  2. Norm-clip delta_i (L2 norm > GRADIENT_CLIP_THRESHOLD → clip)      │
│  3. Cosine outlier detection (similarity < threshold → exclude)        │
│  4. Weighted FedAvg:  p_global = Σ (n_i/N_total) × p_i               │
│  5. Server-side DP noise injection (defence-in-depth)                  │
│  6. Broadcast p_global to all participants                             │
│  7. Write signed audit record to SQLite                                │
└──────────────────────────────────────────────────────────┬────────────┘
                                                           │ p_global
                                                           ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      Exchange Operator (distillation)                  │
│                                                                        │
│  Fine-tune local ensemble on:                                          │
│    • Private data (X_priv, y_priv)                                    │
│    • Public dataset annotated with distilled labels (X_pub, p_global) │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Gradient Representation: Option B — Knowledge Distillation

### Why Option B?

LedgerLens trains three heterogeneous tree-ensemble classifiers: `RandomForestClassifier`, `XGBClassifier`, and `LGBMClassifier`.

**Option A (leaf-value FedAvg)** requires serialising internal tree leaf arrays.  This is feasible for XGBoost and LightGBM (both expose leaf-value APIs) but not for scikit-learn's RandomForest, which would need to be dropped or replaced.  Combining leaf arrays from different model types also requires a shared architecture assumption that doesn't exist here.

**Option C (MLP head + FedAvg on NN weights)** introduces a fourth model component with its own training dynamics, hyperparameters, and maintenance burden.  It also requires gradient back-propagation through the tree-encoded leaf features, which is non-standard.

**Option B (Knowledge Distillation)** works uniformly across all three classifier families:

1. A **shared public synthetic dataset** `X_pub` is generated from `ingestion.synthetic_data.generate_synthetic_dataset(seed=0)` — identical for every participant.
2. Each participant runs its local ensemble on `X_pub` to produce a **soft-label vector** `p_i ∈ [0,1]^N`.
3. The server computes the **weighted FedAvg** of soft labels: `p_global = Σ (n_i/N_total) × p_i`.
4. Participants **retrain** their local ensembles on their private data **augmented** with `(X_pub, round(p_global))` as an additional training source.

The "gradient update" analogue in this scheme is `delta_i = p_i - p_global_prev`, which is:
- A well-defined vector in `R^N` supporting L2 norm clipping and cosine similarity comparison.
- Computed entirely from predictions on a *public* dataset — no private data is encoded.
- Compatible with XGBoost/LightGBM warm-starting via `xgb_model=` / `init_model=`.

### Privacy Properties

- **No raw transaction data leaves the operator.**
- Soft labels on a *public synthetic* dataset carry minimal information about private distribution. Unlike gradients from training data directly (as in neural-network FedAvg), predictions on a fixed public set have bounded sensitivity.
- The Gaussian mechanism provides `(ε, δ)`-DP guarantees on the transmitted update.

### Performance Trade-offs

| Dimension | KD (Option B) | Leaf-value (A) | MLP head (C) |
|-----------|--------------|----------------|--------------|
| Works with RF | ✓ | ✗ | ✓ |
| Architecture coupling | None | High | Moderate |
| Communication cost | O(N_pub) floats | O(n_trees × n_leaves) | O(MLP params) |
| Privacy analysis | Clean | Complex | Standard |
| First-round quality | Depends on public data quality | Depends on tree depth | Depends on MLP capacity |

---

## Differential Privacy

### Gaussian Mechanism

Each participant adds zero-mean Gaussian noise to their gradient update before transmission:

```
σ = clip_threshold × √(2 × ln(1.25/δ)) / ε
noise ~ N(0, σ²)
delta_noisy = clip(delta, clip_threshold) + noise
```

The server applies a second independent noise injection after aggregation (**defence-in-depth**):

```
p_global_noisy = FedAvg(p_i_noisy) + N(0, σ²)
```

### Double-Noise Composition

When both client and server inject `(ε, δ)`-DP Gaussian noise, the combined mechanism satisfies `(ε_total, δ_total)`-DP under basic composition:

```
ε_total ≤ ε_client + ε_server = 2ε
δ_total ≤ δ_client + δ_server = 2δ
```

This is conservative. Rényi DP (RDP) accounting would give a tighter bound, particularly for many rounds. The implementation currently uses basic composition; upgrading to RDP or the PRV accountant (Gopi et al., 2021) would tighten the budget estimate without changing the mechanism.

### Privacy Budget Accounting

Each round consumes `FEDERATED_DP_EPSILON` from the cumulative privacy budget.  When `cumulative_ε ≥ FEDERATED_DP_MAX_EPSILON`, the server rejects all new updates and raises a `RuntimeError`.  Operators must acknowledge the budget exhaustion (e.g. via admin intervention or reconfiguring `FEDERATED_DP_MAX_EPSILON`) before new rounds can proceed.

Cumulative ε is persisted in the `federated_audit_log` SQLite table across server restarts.

---

## Security Model

### What the server learns
- Weighted averages of soft-label predictions on a *public synthetic* dataset.
- The L2 norm of each participant's update (logged in the audit record).
- Each round's cumulative privacy budget.

### What the server does NOT learn
- Raw transaction data.
- Private model weights or tree structure.
- Exact prediction probabilities before DP noise is applied (client applies noise first).

### What participants learn
- The aggregated soft labels `p_global` (weighted average of all participants' noisy predictions on the public dataset).
- The server's public key (for audit verification).

### Authentication
Each participant generates an Ed25519 keypair:

```bash
# Generate keypair (example using Python)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
sk = Ed25519PrivateKey.generate()
```

The public key is registered with the server at onboarding. Every gradient update is signed with the participant's private key; the server verifies the signature before processing.

### Gradient Poisoning Defences

1. **Norm clipping**: Any update with `‖delta‖₂ > GRADIENT_CLIP_THRESHOLD` is clipped to the threshold. The participant hash and clip event are logged (WARNING level).
2. **Cosine similarity outlier detection**: The server maintains a running mean of previous-round gradients. If `cos(delta_i, mean_delta) < GRADIENT_OUTLIER_THRESHOLD`, the update is excluded from aggregation and the exclusion is recorded in the audit log.

---

## Operator Onboarding

1. **Generate keypair**
   ```bash
   python3 -c "
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
   from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat
   sk = Ed25519PrivateKey.generate()
   print('PRIVATE:', sk.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode())
   print('PUBLIC DER (b64):', __import__('base64').b64encode(sk.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode())
   "
   ```

2. **Register with the server** (server operator registers your public key):
   ```bash
   curl -X POST http://server:8001/federated/register \
     -H 'Content-Type: application/json' \
     -d '{"participant_id":"exchange-xyz","public_key_der_b64":"<your_public_key>"}'
   ```

3. **Participate in a round**:
   ```bash
   python3 cli.py federated join \
     --operator-id exchange-xyz \
     --data-path /path/to/private/labelled_data.csv \
     --server-url http://server:8001 \
     --rounds 5
   ```
   The CSV must include columns matching `FEATURE_NAMES` (from `detection/feature_engineering.py`) plus a `label` column (0/1).

4. **Start the federated server** (server operator):
   ```bash
   python3 cli.py federated server --host 0.0.0.0 --port 8001 --min-participants 3
   ```

---

## Admin API

### Audit Log

```
GET /admin/federated/audit-log?limit=50
Authorization: X-Admin-Key: <LEDGERLENS_ADMIN_API_KEY>
```

Returns a list of signed audit records. Each record contains:
- `round_id`: UUID of the federated round.
- `participants`: list of SHA-256 hashes of participant IDs (never plaintext).
- `excluded_participants`: participants excluded due to gradient poisoning.
- `aggregated_update_norm`: L2 norm of the aggregated gradient.
- `dp_epsilon_consumed`: privacy budget consumed in this round.
- `cumulative_epsilon`: total ε consumed across all rounds.
- `timestamp`: ISO-8601 UTC timestamp.
- `_signature_hex`: server's Ed25519 signature for offline verification.

### Verifying an audit record offline

```python
from detection.federated.audit import verify_record
from cryptography.hazmat.primitives.serialization import load_der_public_key
import base64, json

pub_key = load_der_public_key(base64.b64decode(server_public_key_der_b64))
record = { ... }  # from the audit-log API (omit _signature_hex field)
sig = bytes.fromhex(record.pop("_signature_hex"))
assert verify_record(record, sig, pub_key), "Tampered!"
```

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `FEDERATED_MIN_PARTICIPANTS` | `3` | Quorum before aggregation |
| `FEDERATED_DP_EPSILON` | `1.0` | Per-round ε (Gaussian mechanism) |
| `FEDERATED_DP_DELTA` | `1e-5` | Per-round δ (Gaussian mechanism) |
| `FEDERATED_DP_MAX_EPSILON` | `10.0` | Max cumulative ε before halt |
| `GRADIENT_CLIP_THRESHOLD` | `10.0` | L2 norm clip threshold |
| `GRADIENT_OUTLIER_THRESHOLD` | `0.1` | Cosine similarity exclusion threshold |
| `FEDERATED_SERVER_HOST` | `127.0.0.1` | Server bind host |
| `FEDERATED_SERVER_PORT` | `8001` | Server bind port |
