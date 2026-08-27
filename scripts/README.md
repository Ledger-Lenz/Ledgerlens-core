# scripts/

Standalone operational scripts. These are **not** imported by the application —
each is a manual entry point a contributor runs directly in a specific
situation.

| Script | Kind | When you need it |
|--------|------|------------------|
| `setup_trusted_ceremony.sh` | Cryptographic ceremony | (Re)generating the Groth16 proving/verification keys for the zk-SNARK range-proof backend |
| `train_gnn.py` | Model training | Training / retraining the GNN ring-membership classifier |
| `train_lstm_autoencoder.py` | Model training | Training / retraining the LSTM autoencoder used for temporal wash-trade detection |

Run everything from the repository root. The two Python scripts insert the
project root on `sys.path` themselves, so `python scripts/<name>.py` works
without `pip install -e .` — but they still need their heavy ML dependencies
(see each section).

---

## `setup_trusted_ceremony.sh`

### Purpose

Runs a full Groth16 trusted-setup ceremony (Powers-of-Tau Phase 1 + circuit
Phase 2) for the `score_range_proof` circuit and produces the key material the
zk-SNARK range-proof backend loads at runtime:

- `circuits/keys/score_range_proof.zkey` — proving key
- `circuits/keys/verification_key.json` — verification key

You run this when bootstrapping the zk-SNARK backend for the first time, or when
rotating keys after a circuit change. Most contributors never need it.

### Requirements

- [`snarkjs`](https://github.com/iden3/snarkjs) installed globally (`npm i -g snarkjs`)
- [`circom`](https://docs.circom.io/) on `PATH`
- `circuits/score_range_proof.circom` present (it is, in this repo)
- `bash` with `set -euo pipefail` support

### Invocation

```bash
bash scripts/setup_trusted_ceremony.sh
```

No flags, no environment variables. Fixed parameters baked into the script:

- Curve `bn128`, Powers-of-Tau size `2^12` (`snarkjs powersoftau new bn128 12`)
- Circuit name `score_range_proof`, output dir `circuits/`, keys dir `circuits/keys/`
- Three Phase-1 contributions plus one Phase-2 contribution, all with
  **hard-coded entropy strings** (`random_entropy_source_1`, …). This is fine for
  local development only — a real ceremony must supply fresh, secret entropy per
  contributor and keep the contribution transcripts.

The script `mkdir -p`s the keys directory, writes `pot12_*.ptau` scratch files in
the working directory during the run, and deletes them (and the intermediate
`*_0000.zkey`) on success.

### Related docs

- [`docs/zk_snark_range_proof.md`](../docs/zk_snark_range_proof.md) — circuit
  design and the "Trusted Setup Ceremony" section this script implements
- [`docs/zk_proofs.md`](../docs/zk_proofs.md) — the range-proof feature overall
- [`docs/zk_verifier_gas.md`](../docs/zk_verifier_gas.md) — on-chain verifier cost

---

## `train_gnn.py`

### Purpose

Trains `GNNRingDetector` (a GraphSAGE encoder + MLP classifier from
`detection/gnn_ring_detector.py`) to score wallets for wash-ring membership.
Positive labels come from the `ring_members` table (`confirmed = 1`); negatives
are low-risk wallets (`wallet_scores.score < 20` in the last 30 days) with no
open alert in the last 90 days, downsampled to
`len(positives) * neg_sample_ratio`.

Run it to (re)produce `models/gnn_ring_detector.pt` when labels change or the
model architecture is updated.

### Requirements

- `torch` and `torch-geometric` (`pip install torch-geometric`). The script
  exits early with an install hint if PyTorch Geometric is missing.
- `scikit-learn` (already a project dependency) for `roc_auc_score`.
- A LedgerLens SQLite DB with `ring_members` and `wallet_scores` tables. Without
  it the script logs a warning and exits non-zero on "insufficient labelled
  data".

### Invocation

```bash
python scripts/train_gnn.py --help          # full flag reference
python scripts/train_gnn.py --epochs 50 --lr 0.001 --neg-sample-ratio 3
python scripts/train_gnn.py --graph-mode heterogeneous --conv-type sage
```

Key flags (all have `argparse` defaults; run `--help` for the complete list):

| Flag | Default | Meaning |
|------|---------|---------|
| `--db-path` | `$LEDGERLENS_DB_PATH` or `./ledgerlens.db` | SQLite DB with the label tables |
| `--model-path` | `$GNN_MODEL_PATH` or `models/gnn_ring_detector.pt` | Output checkpoint (`.pt`); a `.sha256` sidecar is written next to it |
| `--epochs` | `50` | Max training epochs (early stopping, patience 5, on val AUC-ROC) |
| `--lr` | `0.001` | Adam learning rate |
| `--neg-sample-ratio` | `3` | Negatives per positive; also the BCE `pos_weight` |
| `--graph-mode` | `homogeneous` | `homogeneous` (wallet-only) or `heterogeneous` (wallet+asset+order) |
| `--conv-type` | `sage` | `sage` or `hgt`, only used in heterogeneous mode |

Environment: `LEDGERLENS_DB_PATH` and `GNN_MODEL_PATH` are read as defaults for
`--db-path` / `--model-path`.

Output: encoder + classifier state dicts and training metadata saved to the
`--model-path` checkpoint, plus a SHA-256 checksum file alongside it.

### Related docs

- [`docs/gnn_ring_detection.md`](../docs/gnn_ring_detection.md) — model
  architecture, graph schema, and training-data definition
- [`docs/model_cards.md`](../docs/model_cards.md),
  [`docs/model_signing.md`](../docs/model_signing.md) — model card and checksum
  conventions

---

## `train_lstm_autoencoder.py`

### Purpose

Trains `LSTMAutoencoder` (`detection/temporal_patterns.py`) on **clean**
(non-wash-trade) wallet trade sequences so it reconstructs normal behaviour
well. At inference time, high reconstruction loss flags anomalous / wash-trade
sequences (Issue #298).

Training data is 5-minute-binned `(log_amount, trade_count)` sequences pulled
from `feature_distribution_snapshots` in the DB; if that query returns nothing
(or the DB is unavailable) the script **falls back to 500 synthetic sequences**
so a run always completes.

Run it to (re)produce `models/lstm_autoencoder.pt`.

### Requirements

- `torch` (`pip install torch`). The script exits with an error if PyTorch is
  missing.
- `numpy` (project dependency).
- Optionally a LedgerLens SQLite DB with `feature_distribution_snapshots`; not
  required thanks to the synthetic fallback.

### Invocation

```bash
python scripts/train_lstm_autoencoder.py --help    # full flag reference
python scripts/train_lstm_autoencoder.py \
    --epochs 100 --lr 0.001 --db-path ledgerlens.db --model-dir models \
    --hidden-dim 64 --num-layers 2 --dropout 0.2 \
    --sequence-length 48 --batch-size 32 --val-split 0.2
```

Key flags (all have `argparse` defaults; run `--help` for the complete list):

| Flag | Default | Meaning |
|------|---------|---------|
| `--epochs` | `100` | Max training epochs (early stopping, `--patience` default 10, on val loss) |
| `--lr` | `1e-3` | Adam learning rate |
| `--db-path` | `ledgerlens.db` | SQLite DB; synthetic data used if empty/missing |
| `--model-dir` | `models` | Output dir — writes `lstm_autoencoder.pt`, `lstm_autoencoder.sha256`, `lstm_training_metadata.json` |
| `--hidden-dim` | `64` | LSTM hidden dimension |
| `--num-layers` | `2` | Stacked LSTM layers |
| `--sequence-length` | `48` | Sequence length in 5-min bins (48 = 4 h) |
| `--batch-size` | `32` | Training batch size |
| `--val-split` | `0.2` | Validation fraction |
| `--seed` | `42` | Torch / `random` seed |
| `--neg-sample-ratio` | `3` | Informational only — not used in the training loop |

No environment variables are consulted.

Output: model state-dict + architecture metadata at
`{model_dir}/lstm_autoencoder.pt`, a SHA-256 checksum, and a
`lstm_training_metadata.json` recording val loss, sequence count, epochs run, and
the full arg set.

### Related docs

- [`docs/temporal_model.md`](../docs/temporal_model.md) — temporal sequence model
  design and per-trade feature encoding
- [`docs/model_cards.md`](../docs/model_cards.md),
  [`docs/model_signing.md`](../docs/model_signing.md) — model card and checksum
  conventions

---

_Verified on 2026-08-27 by re-reading all three scripts and running
`python scripts/train_gnn.py --help` and
`python scripts/train_lstm_autoencoder.py --help`._
