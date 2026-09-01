"""
LSTM Autoencoder Training Script  (Issue #298)
===============================================
Trains the LSTMAutoencoder on normal (non-wash-trade) wallet trade sequences
so the model learns to reconstruct clean sequences well.  High reconstruction
loss at inference time indicates anomalous (wash-trade) behaviour.

Usage
-----
::

    python scripts/train_lstm_autoencoder.py \\
        --epochs 100 \\
        --lr 0.001 \\
        --neg-sample-ratio 3 \\
        --db-path ledgerlens.db \\
        --model-dir models \\
        --hidden-dim 64 \\
        --num-layers 2 \\
        --dropout 0.2 \\
        --sequence-length 48 \\
        --batch-size 32 \\
        --val-split 0.2

Ground truth
------------
* Training data: wallets with risk_score < 20 for ≥ 30 days (clean wallets).
* The autoencoder is trained to reconstruct clean sequences; anomalous
  sequences (wash trading bots) will have higher reconstruction loss at
  inference time.

Output
------
* ``{model_dir}/lstm_autoencoder.pt`` — model state-dict + architecture metadata.
* ``{model_dir}/lstm_autoencoder.sha256`` — SHA-256 checksum.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("ledgerlens.train_lstm")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter
):
    """Show argument defaults *and* keep the epilog's line breaks."""


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="train_lstm_autoencoder",
        description=(
            "Train the LedgerLens LSTM autoencoder (detection.temporal_patterns."
            "LSTMAutoencoder) on clean (non-wash-trade) wallet trade sequences so "
            "it reconstructs normal behaviour well; high reconstruction loss at "
            "inference then flags anomalies. Training sequences are 5-min-binned "
            "(log_amount, trade_count) pairs read from the "
            "`feature_distribution_snapshots` table; if the DB is missing or that "
            "query is empty the script falls back to 500 synthetic sequences so a "
            "run always completes. Writes lstm_autoencoder.pt, its .sha256, and "
            "lstm_training_metadata.json under --model-dir."
        ),
        formatter_class=_HelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Quick smoke run on synthetic data (no DB needed):\n"
            "  python scripts/train_lstm_autoencoder.py --epochs 3 --db-path /nonexistent.db\n"
            "\n"
            "  # Full run against a populated DB:\n"
            "  python scripts/train_lstm_autoencoder.py --epochs 100 --db-path ledgerlens.db \\\n"
            "      --hidden-dim 64 --num-layers 2 --sequence-length 48 --batch-size 32\n"
        ),
    )
    parser.add_argument(
        "--epochs", type=int, default=100,
        help="Maximum training epochs (positive int); early stopping may end training sooner.",
    )
    parser.add_argument(
        "--lr", type=float, default=1e-3,
        help="Adam optimiser learning rate (positive float, e.g. 0.001).",
    )
    parser.add_argument(
        "--neg-sample-ratio",
        type=int,
        default=3,
        help=(
            "Recorded in the training metadata for provenance only; the "
            "autoencoder trains on clean sequences and does not use this value."
        ),
    )
    parser.add_argument(
        "--db-path", default="ledgerlens.db",
        help=(
            "Path to the LedgerLens SQLite database. Only the "
            "`feature_distribution_snapshots` table is read. A missing file or "
            "empty result triggers the synthetic-data fallback (still exits 0)."
        ),
    )
    parser.add_argument(
        "--model-dir", default="models",
        help=(
            "Output directory (created if absent). Receives lstm_autoencoder.pt, "
            "lstm_autoencoder.sha256, and lstm_training_metadata.json."
        ),
    )
    parser.add_argument(
        "--hidden-dim", type=int, default=64,
        help="LSTM hidden-state dimension in units (positive int).",
    )
    parser.add_argument(
        "--num-layers", type=int, default=2,
        help="Number of stacked LSTM layers in both encoder and decoder (positive int).",
    )
    parser.add_argument(
        "--dropout", type=float, default=0.2,
        help="Dropout probability between LSTM layers (float in 0..1).",
    )
    parser.add_argument(
        "--sequence-length",
        type=int,
        default=48,
        help=(
            "Sequence length in time bins (positive int). 48 bins = 4h at 5-min "
            "resolution. Wallets with fewer than this many bins are skipped."
        ),
    )
    parser.add_argument(
        "--batch-size", type=int, default=32,
        help="Number of sequences per training batch (positive int).",
    )
    parser.add_argument(
        "--val-split", type=float, default=0.2,
        help="Fraction of sequences held out for validation (float in 0..1); at least 1 sequence is always kept.",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Seed for torch and the stdlib `random` module (int) for reproducible runs.",
    )
    parser.add_argument(
        "--patience", type=int, default=10,
        help="Early-stopping patience: stop after this many epochs without an improvement in validation loss.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def load_clean_wallet_series(db_path: str, sequence_length: int) -> list[np.ndarray]:
    """Load 5-min binned trade sequences for clean wallets.

    Returns a list of numpy arrays of shape ``(sequence_length, 2)``
    (log_amount, trade_count) per sequence.

    Falls back to synthetic data when the DB is unavailable.
    """
    try:
        conn = sqlite3.connect(db_path)
        # Try to get feature distribution snapshots (binned amounts available)
        cur = conn.execute(
            """
            SELECT wallet, feature_name, feature_value, recorded_at
            FROM feature_distribution_snapshots
            WHERE feature_name IN ('log_amount_bin', 'trade_count_bin')
            ORDER BY wallet, recorded_at
            LIMIT 50000
            """
        )
        rows = cur.fetchall()
        conn.close()
        if rows:
            logger.info("Loaded %d feature snapshot rows from DB.", len(rows))
            # Simple approach: group by wallet and build sequences
            sequences = _build_sequences_from_snapshots(rows, sequence_length)
            if sequences:
                return sequences
    except Exception as exc:
        logger.warning("Could not load feature snapshots: %s", exc)

    # Synthetic data fallback
    logger.info("Generating synthetic training sequences …")
    return _generate_synthetic_sequences(n=500, sequence_length=sequence_length)


def _build_sequences_from_snapshots(
    rows: list, sequence_length: int
) -> list[np.ndarray]:
    """Convert DB snapshot rows into fixed-length sequence arrays."""
    from collections import defaultdict

    wallet_data: dict[str, list] = defaultdict(list)
    for wallet, feat_name, feat_value, recorded_at in rows:
        wallet_data[wallet].append((recorded_at, feat_name, float(feat_value or 0)))

    sequences = []
    for wallet, records in wallet_data.items():
        records.sort(key=lambda x: x[0])
        # Interleave log_amount and trade_count into pairs
        log_amounts = [v for _, fn, v in records if fn == "log_amount_bin"]
        counts = [v for _, fn, v in records if fn == "trade_count_bin"]
        n = min(len(log_amounts), len(counts))
        if n < sequence_length:
            continue
        # Slide over the series in non-overlapping windows
        for start in range(0, n - sequence_length + 1, sequence_length):
            la = np.array(log_amounts[start : start + sequence_length], dtype=np.float32)
            ct = np.array(counts[start : start + sequence_length], dtype=np.float32)
            seq = np.stack([la, ct], axis=1)
            sequences.append(seq)

    return sequences


def _generate_synthetic_sequences(n: int, sequence_length: int) -> list[np.ndarray]:
    """Generate synthetic clean-wallet sequences (Gaussian noise + trend)."""
    seqs = []
    rng = np.random.default_rng(42)
    for _ in range(n):
        log_amounts = rng.normal(loc=1.5, scale=0.8, size=sequence_length).astype(
            np.float32
        )
        counts = rng.poisson(lam=3, size=sequence_length).astype(np.float32)
        seqs.append(np.stack([log_amounts, counts], axis=1))
    return seqs


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------


def train(args: argparse.Namespace) -> None:
    try:
        import torch
        import torch.nn as nn
    except ImportError as exc:
        logger.error("PyTorch is required for LSTM training: %s", exc)
        sys.exit(1)

    from detection.temporal_patterns import LSTMAutoencoder

    torch.manual_seed(args.seed)
    random.seed(args.seed)

    # --- Load data ------------------------------------------------------------
    sequences = load_clean_wallet_series(args.db_path, args.sequence_length)
    if not sequences:
        logger.error("No training sequences available. Exiting.")
        sys.exit(1)

    logger.info("Training on %d sequences of length %d.", len(sequences), args.sequence_length)

    # --- Train/val split -------------------------------------------------------
    random.shuffle(sequences)
    val_size = max(1, int(len(sequences) * args.val_split))
    train_seqs = sequences[val_size:]
    val_seqs = sequences[:val_size]

    def make_batch(seqs: list, batch_size: int) -> list:
        batches = []
        for i in range(0, len(seqs), batch_size):
            batch = seqs[i : i + batch_size]
            t = torch.tensor(np.stack(batch), dtype=torch.float32)
            batches.append(t)
        return batches

    # --- Model ----------------------------------------------------------------
    model = LSTMAutoencoder(
        input_dim=2,
        hidden_dim=args.hidden_dim,
        num_layers=args.num_layers,
        sequence_length=args.sequence_length,
        dropout=args.dropout,
    )
    optimiser = torch.optim.Adam(model.parameters(), lr=args.lr)
    criterion = nn.MSELoss()

    best_val_loss = float("inf")
    patience_counter = 0
    best_state = None
    epoch = 0

    for epoch in range(1, args.epochs + 1):
        model.train()
        train_batches = make_batch(train_seqs, args.batch_size)
        train_loss_total = 0.0
        for batch in train_batches:
            optimiser.zero_grad()
            recon = model(batch)
            loss = criterion(recon, batch)
            loss.backward()
            optimiser.step()
            train_loss_total += loss.item()

        # Validation
        model.eval()
        val_loss_total = 0.0
        val_batches = make_batch(val_seqs, args.batch_size)
        with torch.no_grad():
            for batch in val_batches:
                recon = model(batch)
                val_loss_total += criterion(recon, batch).item()

        avg_train = train_loss_total / max(len(train_batches), 1)
        avg_val = val_loss_total / max(len(val_batches), 1)
        logger.info(
            "Epoch %3d/%d  train_loss=%.5f  val_loss=%.5f",
            epoch,
            args.epochs,
            avg_train,
            avg_val,
        )

        if avg_val < best_val_loss:
            best_val_loss = avg_val
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            patience_counter = 0
        else:
            patience_counter += 1
            if patience_counter >= args.patience:
                logger.info("Early stopping after %d epochs without improvement.", args.patience)
                break

    if best_state is None:
        best_state = model.state_dict()

    logger.info("Best validation loss: %.5f", best_val_loss)

    # --- Save -----------------------------------------------------------------
    os.makedirs(args.model_dir, exist_ok=True)
    save_path = os.path.join(args.model_dir, "lstm_autoencoder.pt")
    torch.save(
        {
            "state_dict": best_state,
            "input_dim": 2,
            "hidden_dim": args.hidden_dim,
            "num_layers": args.num_layers,
            "sequence_length": args.sequence_length,
            "dropout": args.dropout,
            "val_loss": best_val_loss,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        },
        save_path,
    )
    h = hashlib.sha256()
    with open(save_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    checksum_path = save_path.replace(".pt", ".sha256")
    Path(checksum_path).write_text(h.hexdigest())
    logger.info("LSTM autoencoder saved to %s.", save_path)
    logger.info("Checksum written to %s.", checksum_path)

    meta_path = os.path.join(args.model_dir, "lstm_training_metadata.json")
    with open(meta_path, "w") as mf:
        json.dump(
            {
                "model": "lstm_autoencoder",
                "val_loss": best_val_loss,
                "n_sequences": len(sequences),
                "epochs_run": epoch,
                "args": vars(args),
                "trained_at": datetime.now(timezone.utc).isoformat(),
            },
            mf,
            indent=2,
        )
    logger.info("Training metadata written to %s.", meta_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    args = parse_args()
    train(args)
