# Federated Learning in LedgerLens

## Overview

LedgerLens supports a privacy-preserving Federated Learning (FL) mode that allows exchange operators (wallets, custodians, DEX aggregators) to improve the global wash-trading detection model using their private labelled datasets **without sharing raw transaction data**.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                    Operator (out-of-band, once per participant)        │
│  Verify institution identity + rough data volume ──► federated admit  │
│                                       (sets a max_n_samples ceiling)   │
└──────────────────────────────────────────────────────────┬────────────┘
                                                           │
┌──────────────────────────────────────────────────────────┼────────────┐
│                      Exchange Operator (N nodes)          │            │
│                                                            ▼            │
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
│  1. Check DP privacy budget                                            │
│  2. Verify Ed25519 signature                                           │
│  3. Norm-clip delta_i (L2 norm > GRADIENT_CLIP_THRESHOLD → clip)      │
│  4. Cosine outlier detection (similarity < threshold → exclude)        │
│  5. Clamp n_i at this participant's admitted max_n_samples ceiling     │
│  6. Cross-round check: n_i jumped > Nx its own history → exclude       │
│  7. Weighted FedAvg:  p_global = Σ (w_i) × p_i, w_i capped at a       │
│     max fraction of round weight (independent of claimed n_i)         │
│  8. Server-side DP noise injection (defence-in-depth)                  │
│  9. Broadcast p_global to all participants                             │
│ 10. Write signed audit record to SQLite                                │
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

Registration (`POST /federated/register`) is gated on admission by default: a
participant_id must already have been admitted (`federated admit` /
`POST /federated/admit`) or registration is rejected with 403. See
["Participant Admission & Weight Bounding"](#participant-admission--weight-bounding)
below.

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

The public key is registered with the server at onboarding, gated on admission (see below). Every gradient update is signed with the participant's private key; the server verifies the signature before processing.

### Gradient Poisoning Defences

1. **Norm clipping**: Any update with `‖delta‖₂ > GRADIENT_CLIP_THRESHOLD` is clipped to the threshold. The participant hash and clip event are logged (WARNING level).
2. **Cosine similarity outlier detection**: The server maintains a running mean of previous-round gradients. If `cos(delta_i, mean_delta) < GRADIENT_OUTLIER_THRESHOLD`, the update is excluded from aggregation and the exclusion is recorded in the audit log.

Both of these bound the *shape* (magnitude and direction) of a participant's
gradient update. Neither says anything about how much **weight** that
update carries in the aggregate — that is a separate dimension, addressed
below.

---

## Participant Admission & Weight Bounding

### The problem

FedAvg weights each participant's contribution proportionally to its claimed
training-set size: `w_i = n_i / Σ n_j`. This is deliberate — a participant
with genuinely more representative data should influence the global model
more. Historically `n_i` was **entirely self-reported**: registration was
open to any `participant_id`, and the server trusted whatever `n_samples` a
signed update claimed, with no cap. A single registered identity — or many,
Sybil-registered identities — could dominate a round's outcome simply by
claiming a large `n_samples`, regardless of how magnitude/direction-plausible
its gradient was (norm-clipping and cosine-outlier detection do not look at
`n_samples` at all).

### Threat model

- **Sybil registration**: an attacker mints many participant identities to
  collectively acquire weight, or to make one identity's claim look
  corroborated.
- **Single-identity weight inflation**: one registered participant claims an
  unrealistically large `n_samples` to dominate a round outright, while
  submitting a gradient that is individually clip-bounded and
  direction-plausible enough to pass the existing checks.
- **Compromised legitimate participant**: an already-admitted, previously
  well-behaved identity is compromised and suddenly claims far more than its
  own established pattern.

### The fix: admission control + a hard, verified ceiling on claimed weight

Registration (`POST /federated/register`) now requires the `participant_id`
to have been **admitted** first, out-of-band, by an operator —
`cli.py federated admit <id> --max-n-samples <N>` or
`POST /federated/admit` (admin-key gated). Admission does two things:

1. **Closes registration** to a pre-authorized allow-list, directly
   preventing unlimited Sybil identity creation — `federated_admission_required`
   (default `true`) governs this; disabling it restores the old fully-open
   behaviour and is strongly discouraged outside local testing.
2. **Assigns a `max_n_samples` ceiling** the server will *never* credit that
   identity beyond, regardless of what a signed update claims:
   `effective_n_samples = min(claimed_n_samples, admitted_ceiling)`. Over-claims
   are silently clamped (logged at WARNING) rather than rejected outright, so
   an honest participant that mis-estimates its dataset size isn't hard-failed.

On top of the ceiling, two **defense-in-depth** layers guard against cases
where the ceiling itself doesn't (or can't yet) bound things tightly enough:

3. **Per-round weight-share cap** (`FEDERATED_MAX_PARTICIPANT_WEIGHT_FRACTION`,
   default `0.5`): no participant's *effective* weight may exceed this
   fraction of a round's total weight, however large its admitted ceiling —
   see `detection/federated/weighting.py`'s iterative water-filling
   redistribution. This matters when an operator's own admission judgement
   was too generous, or quorum is small enough that one honest-but-large
   participant would otherwise dominate a single round.
4. **Cross-round consistency check** (`FEDERATED_MAX_N_SAMPLES_GROWTH_FACTOR`,
   default `3.0`): a participant's newly claimed (effective) `n_samples` may
   not exceed this multiple of its own historical accepted maximum without
   being excluded for that round (audited, not silently dropped) — catches
   an admitted identity whose behaviour suddenly changes.

### Order of checks (`submit_update`)

DP budget check → signature verification → gradient norm clip → cosine
outlier check → n_samples ceiling clamp → cross-round growth check →
aggregation (weight-share cap applied across all valid updates in the round).
Every check still runs on every submission; none is silently skipped because
another triggered. Norm-clip and cosine-outlier bound the gradient's
shape; the ceiling clamp and growth check bound its *weight*, and the
weight-share cap is the only check applied at the round level (across
participants) rather than per-submission.

### Why not a ZK range proof or TEE attestation?

The issue that motivated this fix suggested evaluating this codebase's
existing Pedersen-commitment / Sigma-protocol machinery
(`detection/zk_commitment.py`, `detection/zk_prover.py`, already used for
hiding a wallet's exact risk score while proving `score ≥ threshold`) for
proving `n_samples` lies in a committed range without revealing it. That
machinery's value comes from the verifier *never learning the underlying
value* — which is exactly what FedAvg cannot afford here: the server must
know each participant's exact `n_samples` to compute `w_i = n_i / Σ n_j` at
all. A zero-knowledge proof that a hidden value is `≤` some ceiling adds no
guarantee beyond a plain, operator-set, signed ceiling checked against a
value the server already sees in the clear — so the added cryptographic
complexity buys nothing here. The same reasoning rules out a TEE-based
attestation: it would prove a value to the server that the server needs
disclosed anyway. This machinery *would* become directly relevant if the
design ever moves to a genuinely blind/hidden-weight secure-aggregation
scheme (the server never sees raw `n_samples`, only proofs of bounded
contribution) — but that is a materially larger undertaking (effectively a
new MPC protocol), is not required to close the threat model described
above, and runs counter to this deployment's "no heavyweight new
infrastructure" constraint.

### Interaction with differential privacy

Admission and weight-bounding are orthogonal to the DP budget accounting in
`detection/federated/privacy_utils.py`: the privacy budget check is the
*first* gate in `submit_update` (unchanged), and cumulative ε is charged per
round based on the mechanism/noise parameters, not on which participants'
updates were accepted or how their weight was capped. A participant excluded
by the ceiling/growth check or capped at aggregation time still had its
signature verified and DP noise budget considerations applied identically to
every other submission that round.

### Interaction with Krum / Multi-Krum (tracked separately, not yet wired in)

`detection/federated/krum.py`'s `KrumAggregator.select`/`KrumStrategy.aggregate`
take a list of gradient vectors (and optional opaque `client_ids` for
logging) and return the most central ones by peer-distance — **they accept no
`n_samples`/weight input at all**. Krum-family algorithms resist Byzantine
*direction* (an adversarial gradient far from the honest cluster), the same
dimension the existing cosine-outlier check targets, not unbounded
self-reported influence *share*. When Krum is wired in as (or alongside) the
aggregation strategy, admission control and the n_samples ceiling remain
necessary exactly as they are today — they operate one layer below gradient
aggregation, bounding *who* may submit and *how much claimed weight* they
carry into whatever aggregation algorithm runs next.

### Residual risk

This fix bounds any *single* participant identity's influence. It does not
and cannot prevent a **fully-colluding set of already-admitted, ceiling- and
cap-respecting participants** from jointly steering the aggregate toward an
agreed adversarial direction — e.g. a majority (or, under the default 0.5
weight cap, potentially just two participants each near the cap) of admitted
identities submitting coordinated gradients that individually pass norm-clip
and cosine-outlier checks. Defending against *colluding-majority* Byzantine
behaviour (as opposed to a single dominant identity) is the province of
Krum/Multi-Krum-style aggregation (tracked separately) and, ultimately, of
how conservatively operators exercise the admission step itself — this fix
does not substitute operator judgement about *who* to admit, only bounds
*how much* any one admitted identity can claim once admitted.

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

2. **Get admitted** (server operator, out-of-band — verify your institution's
   identity and roughly how much labelled data it holds, then set a ceiling):
   ```bash
   python3 cli.py federated admit exchange-xyz --max-n-samples 500000
   # or, against a running server:
   curl -X POST http://server:8001/federated/admit \
     -H 'Content-Type: application/json' -H 'X-LedgerLens-Admin-Key: <admin key>' \
     -d '{"participant_id":"exchange-xyz","max_n_samples":500000,"admitted_by":"jane@ledgerlens-ops"}'
   ```
   Registration is rejected with 403 until this step has run for
   `exchange-xyz`. Choose `--max-n-samples` conservatively — it is the hard
   ceiling on how much aggregation weight this identity can ever claim (see
   "Participant Admission & Weight Bounding" above); it can be raised later
   by re-running `federated admit` with a new value.

3. **Register with the server** (server operator registers your public key):
   ```bash
   curl -X POST http://server:8001/federated/register \
     -H 'Content-Type: application/json' \
     -d '{"participant_id":"exchange-xyz","public_key_der_b64":"<your_public_key>"}'
   ```

4. **Participate in a round**:
   ```bash
   python3 cli.py federated join \
     --operator-id exchange-xyz \
     --data-path /path/to/private/labelled_data.csv \
     --server-url http://server:8001 \
     --rounds 5
   ```
   The CSV must include columns matching `FEATURE_NAMES` (from `detection/feature_engineering.py`) plus a `label` column (0/1). If your dataset exceeds the admitted `max_n_samples` ceiling, the excess is silently clamped for weighting purposes (logged server-side) — it is not an error.

5. **Start the federated server** (server operator):
   ```bash
   python3 cli.py federated server --host 0.0.0.0 --port 8001 --min-participants 3
   ```

---

## Admin API

### Participant Admission

```
POST /federated/admit
X-LedgerLens-Admin-Key: <LEDGERLENS_ADMIN_API_KEY>
{"participant_id": "exchange-xyz", "max_n_samples": 500000, "admitted_by": "jane@ledgerlens-ops"}
```

Served by the federated server process itself (`cli.py federated server`),
not the main `api/main.py` app — this endpoint and `/federated/register` /
`/federated/update` are a separate FastAPI app. Required before
`POST /federated/register` will accept that `participant_id` (unless
`FEDERATED_ADMISSION_REQUIRED=false`).

### Audit Log

```
GET /admin/federated/audit-log?limit=50
Authorization: X-Admin-Key: <LEDGERLENS_ADMIN_API_KEY>
```

Returns a list of signed audit records. Each record contains:
- `round_id`: UUID of the federated round.
- `participants`: list of SHA-256 hashes of participant IDs (never plaintext).
- `excluded_participants`: participants excluded due to gradient poisoning, an over-ceiling clamp being insufficient (see cross-round growth check), or other exclusion reasons.
- `weight_capped_participants`: SHA-256 hashes of participants whose weight was reduced this round by the per-round weight-share cap.
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
| `FEDERATED_ADMISSION_REQUIRED` | `true` | Require operator admission before a participant may register (see "Participant Admission & Weight Bounding") |
| `FEDERATED_MAX_PARTICIPANT_WEIGHT_FRACTION` | `0.5` | Max fraction of a round's total weight any one participant may hold, in `(0.0, 1.0]` |
| `FEDERATED_MAX_N_SAMPLES_GROWTH_FACTOR` | `3.0` | Max multiple of a participant's own historical accepted `n_samples` before a claim is flagged/excluded, `> 1.0` |
| `FEDERATED_SERVER_HOST` | `127.0.0.1` | Server bind host |
| `FEDERATED_SERVER_PORT` | `8001` | Server bind port |
