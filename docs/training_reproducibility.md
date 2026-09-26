# Training reproducibility

LedgerLens training runs are reproducible from a fixed data snapshot and
configuration when run with the same source revision, Python/package versions,
and hardware/backend. Python, NumPy, and PyTorch RNGs are seeded; PyTorch
deterministic algorithms are required, and the ensemble estimators use a single
worker. Unsupported nondeterministic PyTorch operations fail training instead
of silently weakening this guarantee. This is not a promise of bit-for-bit
equivalence across dependency upgrades, CPU/GPU backends, or different hardware.

Every training path records the effective input fingerprint, seed/configuration,
and software versions with its outputs:

| Training path | Provenance output | Fingerprint |
|---|---|---|
| `detection.model_training.train_ensemble` | `training_metadata.json` | Ordered training DataFrame and optional training-reference CSV |
| `scripts/train_gnn.py` | `<model>.training.json` | Sorted positive/selected-negative labels |
| `scripts/train_lstm_autoencoder.py` | `lstm_training_metadata.json` | Ordered sequence tensors actually used, including seeded synthetic fallback data |

The standalone scripts record both a SHA-256 of the serialized checkpoint and
a SHA-256 of the learned state tensors. The ensemble records SHA-256 hashes of
each saved model artifact. A matching data fingerprint and model-state hash
verifies that the same effective data and learned result were reproduced; the
artifact hash additionally verifies byte-for-byte identity of the serialized
file. Checkpoint metadata timestamps are stored outside the model files.

## Re-running standalone trainers

Use a fixed `--seed` and, for GNN training, an explicit timezone-aware `--as-of`
timestamp so the time-windowed negative-label query has a stable anchor:

```bash
python scripts/train_gnn.py \
  --db-path snapshots/ledgerlens.db \
  --model-path models/gnn_ring_detector.pt \
  --seed 42 \
  --as-of 2025-01-31T00:00:00+00:00

python scripts/train_lstm_autoencoder.py \
  --db-path snapshots/ledgerlens.db \
  --model-dir models \
  --seed 42
```

Compare the `dataset_sha256` and `model_state_sha256` values in the generated
metadata files. Verify serialized checkpoints with the adjacent `.sha256`
sidecar (or compare `artifact_sha256` in the metadata).

The ensemble CLI accepts `--seed` and passes it into `train_ensemble` as its
random state. `training_metadata.json` records this seed, the DataFrame
fingerprint, package versions, and saved model hashes.
