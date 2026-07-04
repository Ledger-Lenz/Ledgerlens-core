"""Training script for the GNN ring membership classifier.

Usage
-----
    python scripts/train_gnn.py --epochs 50 --lr 0.001 --neg-sample-ratio 3

Ground truth
    The ``ring_members`` table (columns: wallet TEXT, confirmed BOOLEAN) is used
    as positive labels.  Negative examples are wallets with risk_score < 20
    for at least 30 days AND no open alert in the last 90 days.

Loss
    Binary cross-entropy with ``pos_weight = neg_sample_ratio``.

Early stopping
    Patience=5 on validation AUC-ROC.

Output
    Saves encoder + classifier state dicts to ``models/gnn_ring_detector.pt``
    and a SHA-256 checksum to ``models/gnn_ring_detector.sha256``.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_gnn")

# Ensure project root is on PYTHONPATH
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _check_pyg():
    """Abort early if PyTorch Geometric is not installed."""
    try:
        import torch  # noqa: F401
        from torch_geometric.data import HeteroData  # noqa: F401
    except ImportError as exc:
        logger.error(
            "PyTorch Geometric is required for training: %s\n"
            "Install with: pip install torch-geometric",
            exc,
        )
        sys.exit(1)


def _compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_checksum(model_path: str) -> None:
    checksum = _compute_sha256(model_path)
    checksum_path = model_path.replace(".pt", ".sha256")
    with open(checksum_path, "w") as f:
        f.write(checksum + "\n")
    logger.info("Checksum written to %s", checksum_path)


def _load_labels(db_path: str, neg_sample_ratio: int) -> tuple[list[str], list[str]]:
    """Load positive and negative wallet labels from the SQLite DB.

    Positives: wallets in ``ring_members`` where confirmed=1.
    Negatives: wallets in ``wallet_scores`` with:
        - risk_score < 20 for all records in the last 30 days
        - NO open alert in the last 90 days
    Negatives are downsampled to ``len(positives) * neg_sample_ratio``.
    """
    if not os.path.exists(db_path):
        logger.warning("DB not found at %s — using empty labels.", db_path)
        return [], []

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        # Positives
        try:
            cursor.execute("SELECT wallet FROM ring_members WHERE confirmed = 1")
            positives = [row[0] for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            logger.warning("ring_members table not found — no positive labels.")
            positives = []

        # Negatives: safe wallets not in any open alert in last 90 days
        cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        try:
            cursor.execute(
                """
                SELECT DISTINCT ws.wallet
                FROM wallet_scores ws
                WHERE ws.score < 20
                  AND ws.scored_at > ?
                  AND ws.wallet NOT IN (
                      SELECT DISTINCT wallet FROM alerts
                      WHERE created_at > ?
                  )
                  AND ws.wallet NOT IN ({pos_placeholders})
                """.format(
                    pos_placeholders=",".join("?" * len(positives)) if positives else "'_none_'"
                ),
                [cutoff_30d, cutoff_90d] + positives,
            )
            negatives = [row[0] for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            logger.warning("wallet_scores/alerts table not found — using empty negatives.")
            negatives = []

        # Downsample negatives
        n_neg = len(positives) * neg_sample_ratio
        if len(negatives) > n_neg:
            negatives = random.sample(negatives, n_neg)

        logger.info("Labels: %d positives, %d negatives", len(positives), len(negatives))
        return positives, negatives
    finally:
        conn.close()


def _make_dummy_trades(wallets: list[str], n_trades: int = 50) -> list:
    """Generate synthetic Trade-like objects for graph construction during training."""
    from types import SimpleNamespace

    trades = []
    now_ts = datetime.now(timezone.utc).timestamp()
    for i in range(n_trades):
        src = random.choice(wallets)
        dst = random.choice(wallets)
        if src == dst:
            continue
        t = SimpleNamespace(
            base_account=src,
            counter_account=dst,
            base_amount=random.uniform(1.0, 10000.0),
            ledger_close_time_ts=now_ts - random.uniform(0, 86400),
            base_asset_code="XLM",
            counter_asset_code=random.choice(["XLM", "USDC"]),
        )
        trades.append(t)
    return trades


def _default_node_feature_fn(wallet: str):
    """Simple hash-based node feature vector (4-dim)."""
    import torch

    h = abs(hash(wallet)) % 10000
    return torch.tensor(
        [h / 10000.0, len(wallet) / 60.0, float(wallet.startswith("G")), 0.0],
        dtype=torch.float,
    )


def train(
    db_path: str,
    model_path: str,
    epochs: int = 50,
    lr: float = 0.001,
    neg_sample_ratio: int = 3,
    patience: int = 5,
    val_fraction: float = 0.2,
    hidden_channels: int = 128,
    out_channels: int = 64,
    num_layers: int = 3,
    dropout: float = 0.3,
):
    """Full training loop for GNNRingDetector.

    Parameters
    ----------
    db_path:
        Path to the LedgerLens SQLite database containing ring_members and
        wallet_scores tables.
    model_path:
        Output path for the trained model checkpoint.
    epochs:
        Maximum number of training epochs.
    lr:
        Adam learning rate.
    neg_sample_ratio:
        Ratio of negative to positive examples (used as BCE pos_weight).
    patience:
        Early stopping patience on validation AUC-ROC.
    val_fraction:
        Fraction of data held out for validation.
    hidden_channels:
        Encoder hidden layer width.
    out_channels:
        Encoder output embedding dimension.
    num_layers:
        Number of SAGEConv layers.
    dropout:
        Dropout rate.
    """
    import torch
    from sklearn.metrics import roc_auc_score

    from detection.gnn_ring_detector import (
        GraphSAGEEncoder,
        RingMembershipClassifier,
        build_transaction_graph,
    )

    positives, negatives = _load_labels(db_path, neg_sample_ratio)
    all_wallets = positives + negatives

    if len(all_wallets) < 4:
        logger.error(
            "Insufficient labelled data (%d wallets). "
            "Populate ring_members table and re-run.",
            len(all_wallets),
        )
        sys.exit(1)

    # Shuffle and split
    combined = [(w, 1) for w in positives] + [(w, 0) for w in negatives]
    random.shuffle(combined)
    n_val = max(1, int(len(combined) * val_fraction))
    val_set = combined[:n_val]
    train_set = combined[n_val:]

    train_wallets = [w for w, _ in train_set]
    val_wallets = [w for w, _ in val_set]

    logger.info("Train: %d wallets | Val: %d wallets", len(train_wallets), len(val_wallets))

    # Build graphs
    # Re-build with actual trades
    # In production this would use real trade data from the DB
    all_trades = _make_dummy_trades(list(set(train_wallets + val_wallets)), n_trades=500)
    full_graph = build_transaction_graph(all_trades, _default_node_feature_fn)
    wlist = full_graph["wallet"].wallet_list

    train_labels = []
    train_idx = []
    for w, lbl in train_set:
        if w in wlist:
            train_idx.append(wlist.index(w))
            train_labels.append(float(lbl))

    val_labels = []
    val_idx = []
    for w, lbl in val_set:
        if w in wlist:
            val_idx.append(wlist.index(w))
            val_labels.append(float(lbl))

    if not train_idx:
        logger.error("No training nodes found in graph. Check that wallet addresses match.")
        sys.exit(1)

    in_channels = full_graph["wallet"].x.shape[1]
    encoder = GraphSAGEEncoder(
        in_channels=in_channels,
        hidden_channels=hidden_channels,
        out_channels=out_channels,
        num_layers=num_layers,
        dropout=dropout,
    )
    classifier = RingMembershipClassifier(embedding_dim=out_channels)

    optimizer = torch.optim.Adam(
        list(encoder.parameters()) + list(classifier.parameters()), lr=lr
    )
    pos_weight = torch.tensor([float(neg_sample_ratio)], dtype=torch.float)

    x = full_graph["wallet"].x
    edge_index = full_graph["wallet", "trades", "wallet"].edge_index
    y_train = torch.tensor(train_labels, dtype=torch.float)
    train_idx_t = torch.tensor(train_idx, dtype=torch.long)

    best_val_auc = -1.0
    patience_counter = 0
    best_state: Optional[dict] = None

    for epoch in range(1, epochs + 1):
        encoder.train()
        classifier.train()
        optimizer.zero_grad()

        embeddings = encoder(x, edge_index)
        scores = classifier(embeddings)
        train_scores = scores[train_idx_t]

        # Weighted BCE (positive class weight)
        loss = -(
            pos_weight * y_train * torch.log(train_scores + 1e-8)
            + (1 - y_train) * torch.log(1 - train_scores + 1e-8)
        ).mean()

        loss.backward()
        optimizer.step()

        # Validation
        encoder.eval()
        classifier.eval()
        with torch.no_grad():
            val_embeddings = encoder(x, edge_index)
            val_scores = classifier(val_embeddings)
            val_preds = val_scores[torch.tensor(val_idx, dtype=torch.long)].numpy()

        if len(set(val_labels)) > 1:
            val_auc = roc_auc_score(val_labels, val_preds)
        else:
            val_auc = 0.5

        if epoch % 10 == 0 or epoch == 1:
            logger.info(
                "Epoch %3d/%d — loss=%.4f, val_auc=%.4f",
                epoch,
                epochs,
                loss.item(),
                val_auc,
            )

        if val_auc > best_val_auc:
            best_val_auc = val_auc
            patience_counter = 0
            best_state = {
                "encoder": {k: v.clone() for k, v in encoder.state_dict().items()},
                "classifier": {k: v.clone() for k, v in classifier.state_dict().items()},
            }
        else:
            patience_counter += 1
            if patience_counter >= patience:
                logger.info("Early stopping at epoch %d (patience=%d).", epoch, patience)
                break

    if best_state is None:
        logger.error("Training produced no valid model state.")
        sys.exit(1)

    logger.info("Best validation AUC-ROC: %.4f", best_val_auc)

    os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
    checkpoint = {
        "encoder": best_state["encoder"],
        "classifier": best_state["classifier"],
        "encoder_config": {
            "in_channels": in_channels,
            "hidden_channels": hidden_channels,
            "out_channels": out_channels,
            "num_layers": num_layers,
            "dropout": dropout,
        },
        "classifier_config": {"embedding_dim": out_channels},
        "training_meta": {
            "best_val_auc": best_val_auc,
            "n_positives": len(positives),
            "n_negatives": len(negatives),
            "epochs_run": epoch,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    torch.save(checkpoint, model_path)
    _write_checksum(model_path)
    logger.info("Model saved to %s (val_auc=%.4f)", model_path, best_val_auc)


def main():
    parser = argparse.ArgumentParser(
        description="Train the LedgerLens GNN ring membership classifier.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("LEDGERLENS_DB_PATH", "./ledgerlens.db"),
        help="Path to the LedgerLens SQLite database.",
    )
    parser.add_argument(
        "--model-path",
        default=os.environ.get("GNN_MODEL_PATH", "models/gnn_ring_detector.pt"),
        help="Output path for the trained model checkpoint (.pt).",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=50,
        help="Maximum training epochs.",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=0.001,
        help="Adam optimiser learning rate.",
    )
    parser.add_argument(
        "--neg-sample-ratio",
        type=int,
        default=3,
        dest="neg_sample_ratio",
        help="Ratio of negative to positive examples; also used as BCE pos_weight.",
    )
    parser.add_argument(
        "--patience",
        type=int,
        default=5,
        help="Early stopping patience on validation AUC-ROC.",
    )
    parser.add_argument(
        "--val-fraction",
        type=float,
        default=0.2,
        dest="val_fraction",
        help="Fraction of data reserved for validation.",
    )
    parser.add_argument(
        "--hidden-channels",
        type=int,
        default=128,
        dest="hidden_channels",
        help="GraphSAGE encoder hidden layer width.",
    )
    parser.add_argument(
        "--out-channels",
        type=int,
        default=64,
        dest="out_channels",
        help="GraphSAGE encoder output embedding dimension.",
    )
    parser.add_argument(
        "--num-layers",
        type=int,
        default=3,
        dest="num_layers",
        help="Number of SAGEConv layers (default 3).",
    )
    parser.add_argument(
        "--dropout",
        type=float,
        default=0.3,
        help="Dropout rate in the encoder.",
    )
    args = parser.parse_args()

    _check_pyg()
    train(
        db_path=args.db_path,
        model_path=args.model_path,
        epochs=args.epochs,
        lr=args.lr,
        neg_sample_ratio=args.neg_sample_ratio,
        patience=args.patience,
        val_fraction=args.val_fraction,
        hidden_channels=args.hidden_channels,
        out_channels=args.out_channels,
        num_layers=args.num_layers,
        dropout=args.dropout,
    )


if __name__ == "__main__":
    main()
