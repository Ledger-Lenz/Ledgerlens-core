"""LSTM/Transformer sequence model for wash-trade detection.

Two model classes are provided:

- ``TemporalAnomalyLSTM``  – original score-history LSTM (used by ``run_pipeline.py``
  and ``temporal_dataset.py``; preserved for backward compatibility).
- ``WashTradeSequenceModel`` – new per-trade sequence encoder that processes an ordered
  list of ``Trade`` objects through an LSTM (or Transformer) and fuses the resulting
  embedding with the tabular feature vector from ``feature_engineering.py``.

``TradeSequenceEncoder`` converts ``List[Trade]`` → padded tensor suitable for batch
training with either model variant.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch import Tensor
from torch.nn.utils.rnn import pack_padded_sequence
from torch.utils.data import DataLoader, TensorDataset

logger = logging.getLogger(__name__)

TRADE_FEATURE_DIM = 5  # [log_amount, direction, log_interarrival, asset_id, hour_of_day]

# ---------------------------------------------------------------------------
# Legacy model (preserved for backward compatibility)
# ---------------------------------------------------------------------------


class TemporalAnomalyLSTM(nn.Module):
    """Original score-history LSTM.  Input shape: (batch, seq_len, features)."""

    def __init__(self, input_size: int, hidden_size: int = 64, num_layers: int = 2):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size,
            hidden_size,
            num_layers,
            batch_first=True,
            dropout=0.2 if num_layers > 1 else 0.0,
        )
        self.fc = nn.Linear(hidden_size, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x: Tensor) -> Tensor:
        """x: (batch, seq_len, features) → (batch, 1) risk probability"""
        lstm_out, _ = self.lstm(x)
        return self.sigmoid(self.fc(lstm_out[:, -1, :]))


def train_temporal_model(
    X: np.ndarray,
    y: np.ndarray,
    epochs: int = 15,
    batch_size: int = 32,
    lr: float = 0.001,
) -> TemporalAnomalyLSTM:
    """Train the TemporalAnomalyLSTM model on sequence data."""
    input_size = X.shape[2]
    model = TemporalAnomalyLSTM(input_size=input_size)
    model.train()

    if len(X) == 0:
        return model

    X_tensor = torch.tensor(X, dtype=torch.float32)
    y_tensor = torch.tensor(y, dtype=torch.float32).unsqueeze(1)

    dataset = TensorDataset(X_tensor, y_tensor)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    criterion = nn.BCELoss()
    optimizer = optim.Adam(model.parameters(), lr=lr)

    for _ in range(epochs):
        for batch_X, batch_y in loader:
            optimizer.zero_grad()
            outputs = model(batch_X)
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()

    return model


def predict_temporal_risk(model: TemporalAnomalyLSTM, sequence: np.ndarray) -> float:
    """Predict risk probability for a single sequence of shape (seq_len, features) or (1, seq_len, features)."""
    model.eval()
    if sequence.ndim == 2:
        sequence = np.expand_dims(sequence, axis=0)
    if sequence.shape[0] == 0 or sequence.shape[1] == 0:
        return 0.0
    with torch.no_grad():
        x = torch.tensor(sequence, dtype=torch.float32)
        prob = model(x).item()
    return prob


# ---------------------------------------------------------------------------
# Per-trade feature encoding
# ---------------------------------------------------------------------------


def trade_to_feature_vector(
    trade,
    prev_trade,
    asset_pair_vocab: Dict[str, int],
) -> np.ndarray:
    """Encode a single ``Trade`` as a 5-d float32 feature vector.

    Features
    --------
    0. ``log_amount``        – log1p(|base_amount|)
    1. ``direction``         – 1.0 if base is buyer (not seller), else 0.0
    2. ``log_interarrival``  – log1p(seconds since previous trade, or 0)
    3. ``asset_id``          – normalised integer ID for the asset pair
    4. ``hour_of_day``       – fraction of UTC day [0, 1)

    Args:
        trade:            ``ingestion.data_models.Trade`` (Pydantic-validated).
        prev_trade:       Previous ``Trade`` for the same wallet, or ``None``.
        asset_pair_vocab: Maps asset-pair strings → integer IDs.

    Returns:
        numpy float32 array of shape (5,).
    """
    log_amount = float(np.log1p(abs(trade.base_amount)))
    direction = 0.0 if trade.base_is_seller else 1.0  # buyer = 1.0

    if prev_trade is not None:
        delta_s = (
            trade.ledger_close_time.timestamp()
            - prev_trade.ledger_close_time.timestamp()
        )
        log_interarrival = float(np.log1p(max(0.0, delta_s)))
    else:
        log_interarrival = 0.0

    vocab_size = max(len(asset_pair_vocab), 1)
    asset_id = asset_pair_vocab.get(trade.asset_pair, 0) / vocab_size

    ts = trade.ledger_close_time.timestamp()
    hour_of_day = (ts % 86400) / 86400.0

    return np.array(
        [log_amount, direction, log_interarrival, asset_id, hour_of_day],
        dtype=np.float32,
    )


# ---------------------------------------------------------------------------
# Batch encoder
# ---------------------------------------------------------------------------


class TradeSequenceEncoder:
    """Converts ``List[Trade]`` per wallet into padded tensors for batch training.

    Args:
        asset_pair_vocab: Maps asset-pair strings → integer IDs.  When ``None``
                          the vocabulary is built on first call to ``encode_batch``.
        max_seq_len:      Sequences longer than this are truncated to the most
                          recent ``max_seq_len`` trades (recency bias).
    """

    def __init__(
        self,
        asset_pair_vocab: Optional[Dict[str, int]] = None,
        max_seq_len: int = 200,
    ) -> None:
        self.asset_pair_vocab: Dict[str, int] = asset_pair_vocab or {}
        self.max_seq_len = max_seq_len

    def build_vocab(self, wallet_trades: Dict[str, list]) -> None:
        """Populate ``asset_pair_vocab`` from all trades in the batch."""
        pairs: set = set()
        for trades in wallet_trades.values():
            for t in trades:
                pairs.add(t.asset_pair)
        self.asset_pair_vocab = {p: i for i, p in enumerate(sorted(pairs))}

    def encode_batch(
        self,
        wallet_trades: Dict[str, list],
    ) -> Tuple[Tensor, Tensor]:
        """Encode a dict of ``{wallet: List[Trade]}`` into padded tensors.

        Returns
        -------
        sequences : Tensor of shape (N, max_seq_len, TRADE_FEATURE_DIM)
        lengths   : Tensor of shape (N,) with actual sequence length per wallet
        """
        if not self.asset_pair_vocab:
            self.build_vocab(wallet_trades)

        seqs: List[np.ndarray] = []
        lengths: List[int] = []

        for trades in wallet_trades.values():
            sorted_trades = sorted(trades, key=lambda t: t.ledger_close_time)
            # Recency truncation: keep only the last max_seq_len trades
            sorted_trades = sorted_trades[-self.max_seq_len :]

            features = [
                trade_to_feature_vector(
                    t,
                    sorted_trades[i - 1] if i > 0 else None,
                    self.asset_pair_vocab,
                )
                for i, t in enumerate(sorted_trades)
            ]

            seq = np.zeros((self.max_seq_len, TRADE_FEATURE_DIM), dtype=np.float32)
            if features:
                seq[: len(features)] = np.array(features, dtype=np.float32)

            seqs.append(seq)
            lengths.append(max(len(features), 1))  # at least 1 to keep pack happy

        return (
            torch.tensor(np.stack(seqs), dtype=torch.float32),
            torch.tensor(lengths, dtype=torch.long),
        )

    def save_vocab(self, path: str) -> None:
        """Persist vocabulary to ``path`` as JSON."""
        with open(path, "w") as fh:
            json.dump(self.asset_pair_vocab, fh)

    @classmethod
    def load_vocab(cls, path: str, max_seq_len: int = 200) -> "TradeSequenceEncoder":
        """Load a previously saved vocabulary."""
        with open(path) as fh:
            vocab = json.load(fh)
        return cls(asset_pair_vocab=vocab, max_seq_len=max_seq_len)


# ---------------------------------------------------------------------------
# WashTradeSequenceModel  (LSTM variant)
# ---------------------------------------------------------------------------


class WashTradeSequenceModel(nn.Module):
    """LSTM-based sequence encoder fused with a tabular feature vector.

    The model encodes a wallet's ordered trade sequence via an LSTM, extracts
    the last-layer hidden state as a sequence embedding, concatenates it with
    the tabular feature vector from ``feature_engineering.py``, and passes the
    concatenated representation through a two-layer fusion head that outputs a
    wash-trade probability in [0, 1].

    Args:
        trade_feature_dim:  Per-trade feature dimension (default 5).
        tabular_feature_dim: Number of tabular ML features (default 35 = len(FEATURE_NAMES)).
        lstm_hidden_dim:    LSTM hidden units (default 64).
        lstm_num_layers:    LSTM stacked layers (default 2).
        fusion_hidden_dim:  Hidden units in the fusion MLP (default 32).
        dropout:            Dropout probability (default 0.3).
        max_seq_len:        Maximum sequence length (informational; not enforced here).
    """

    def __init__(
        self,
        trade_feature_dim: int = TRADE_FEATURE_DIM,
        tabular_feature_dim: int = 35,
        lstm_hidden_dim: int = 64,
        lstm_num_layers: int = 2,
        fusion_hidden_dim: int = 32,
        dropout: float = 0.3,
        max_seq_len: int = 200,
    ) -> None:
        super().__init__()
        self.lstm_hidden_dim = lstm_hidden_dim
        self.lstm_num_layers = lstm_num_layers
        self.max_seq_len = max_seq_len

        self.lstm = nn.LSTM(
            input_size=trade_feature_dim,
            hidden_size=lstm_hidden_dim,
            num_layers=lstm_num_layers,
            batch_first=True,
            dropout=dropout if lstm_num_layers > 1 else 0.0,
            bidirectional=False,
        )
        self.fusion = nn.Sequential(
            nn.Linear(lstm_hidden_dim + tabular_feature_dim, fusion_hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(fusion_hidden_dim, 1),
            nn.Sigmoid(),
        )

    def forward(
        self,
        sequences: Tensor,
        seq_lengths: Tensor,
        tabular: Tensor,
    ) -> Tensor:
        """Forward pass.

        Args:
            sequences:   (batch, max_seq_len, trade_feature_dim) – padded sequences.
            seq_lengths: (batch,) – actual length of each sequence (CPU tensor).
            tabular:     (batch, tabular_feature_dim) – tabular feature vectors.

        Returns:
            (batch,) – wash-trade probability per wallet, in [0, 1].
        """
        # Clamp lengths to [1, max_seq_len] to satisfy pack_padded_sequence
        lengths_cpu = seq_lengths.cpu().clamp(min=1, max=sequences.size(1))
        packed = pack_padded_sequence(
            sequences, lengths_cpu, batch_first=True, enforce_sorted=False
        )
        _, (h_n, _) = self.lstm(packed)
        seq_embedding = h_n[-1]  # last layer: (batch, lstm_hidden_dim)
        fused = torch.cat([seq_embedding, tabular], dim=1)
        return self.fusion(fused).squeeze(-1)


# ---------------------------------------------------------------------------
# Transformer variant (selectable via TEMPORAL_MODEL_TYPE="transformer")
# ---------------------------------------------------------------------------


class _LearnedPositionalEncoding(nn.Module):
    def __init__(self, d_model: int, max_len: int = 200) -> None:
        super().__init__()
        self.pe = nn.Embedding(max_len, d_model)

    def forward(self, x: Tensor) -> Tensor:
        positions = torch.arange(x.size(1), device=x.device).unsqueeze(0)
        return x + self.pe(positions)


class TransformerSequenceModel(nn.Module):
    """Transformer-based variant of ``WashTradeSequenceModel``.

    Prepends a learnable [CLS] token; its output embedding is used as the
    sequence representation fused with the tabular vector.

    Args:
        trade_feature_dim:  Per-trade feature dimension (default 5).
        tabular_feature_dim: Number of tabular ML features (default 35).
        d_model:            Transformer internal dimension (default 64).
        nhead:              Number of attention heads (default 4).
        num_layers:         Number of encoder layers (default 2).
        fusion_hidden_dim:  Fusion MLP hidden units (default 32).
        dropout:            Dropout probability (default 0.3).
        max_seq_len:        Maximum sequence length (default 200).
    """

    def __init__(
        self,
        trade_feature_dim: int = TRADE_FEATURE_DIM,
        tabular_feature_dim: int = 35,
        d_model: int = 64,
        nhead: int = 4,
        num_layers: int = 2,
        fusion_hidden_dim: int = 32,
        dropout: float = 0.3,
        max_seq_len: int = 200,
    ) -> None:
        super().__init__()
        self.d_model = d_model
        self.max_seq_len = max_seq_len

        self.input_proj = nn.Linear(trade_feature_dim, d_model)
        self.pos_encoding = _LearnedPositionalEncoding(d_model, max_len=max_seq_len + 1)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, d_model))

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, batch_first=True, dropout=dropout
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        self.fusion = nn.Sequential(
            nn.Linear(d_model + tabular_feature_dim, fusion_hidden_dim),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(fusion_hidden_dim, 1),
            nn.Sigmoid(),
        )

    def forward(
        self,
        sequences: Tensor,
        seq_lengths: Tensor,
        tabular: Tensor,
    ) -> Tensor:
        """Forward pass.

        Args:
            sequences:   (batch, max_seq_len, trade_feature_dim).
            seq_lengths: (batch,) – actual per-sequence lengths (for padding mask).
            tabular:     (batch, tabular_feature_dim).

        Returns:
            (batch,) – wash-trade probability in [0, 1].
        """
        B, S, _ = sequences.shape
        x = self.input_proj(sequences)  # (B, S, d_model)

        # Prepend [CLS] token
        cls = self.cls_token.expand(B, -1, -1)  # (B, 1, d_model)
        x = torch.cat([cls, x], dim=1)          # (B, S+1, d_model)
        x = self.pos_encoding(x)

        # Build padding mask: True where position should be *ignored*
        # Position 0 = CLS (never masked); positions 1..S masked when padded
        mask = torch.ones(B, S + 1, dtype=torch.bool, device=sequences.device)
        mask[:, 0] = False  # CLS is always attended
        for i, length in enumerate(seq_lengths):
            attend = int(length.item()) + 1  # +1 for CLS
            mask[i, :attend] = False

        x = self.transformer(x, src_key_padding_mask=mask)
        cls_embedding = x[:, 0, :]  # (B, d_model)

        fused = torch.cat([cls_embedding, tabular], dim=1)
        return self.fusion(fused).squeeze(-1)


# ---------------------------------------------------------------------------
# Model factory
# ---------------------------------------------------------------------------


def build_sequence_model(
    model_type: str = "lstm",
    tabular_feature_dim: int = 35,
    lstm_hidden_dim: int = 64,
    max_seq_len: int = 200,
    **kwargs,
) -> nn.Module:
    """Instantiate a sequence model by type string.

    Args:
        model_type:          ``"lstm"`` or ``"transformer"``.
        tabular_feature_dim: Number of tabular features (default 35).
        lstm_hidden_dim:     LSTM hidden dimension (default 64).
        max_seq_len:         Maximum sequence length (default 200).

    Returns:
        A ``WashTradeSequenceModel`` or ``TransformerSequenceModel`` instance.
    """
    if model_type == "transformer":
        return TransformerSequenceModel(
            tabular_feature_dim=tabular_feature_dim,
            d_model=lstm_hidden_dim,
            max_seq_len=max_seq_len,
            **kwargs,
        )
    return WashTradeSequenceModel(
        tabular_feature_dim=tabular_feature_dim,
        lstm_hidden_dim=lstm_hidden_dim,
        max_seq_len=max_seq_len,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

_TEMPORAL_MODEL_FILENAME = "temporal_model.pt"
_VOCAB_FILENAME = "asset_pair_vocab.json"


def save_sequence_model(model: nn.Module, model_dir: str) -> None:
    """Save model state dict to ``<model_dir>/temporal_model.pt``."""
    os.makedirs(model_dir, exist_ok=True)
    path = os.path.join(model_dir, _TEMPORAL_MODEL_FILENAME)
    torch.save(model.state_dict(), path)
    logger.info("Saved WashTradeSequenceModel to %s", path)


def load_sequence_model(
    model_dir: str,
    model_type: str = "lstm",
    tabular_feature_dim: int = 35,
    lstm_hidden_dim: int = 64,
    max_seq_len: int = 200,
) -> Optional[nn.Module]:
    """Load ``temporal_model.pt`` from ``model_dir``, returning ``None`` if absent.

    Uses ``weights_only=True`` to prevent pickle-based code execution.
    """
    path = os.path.join(model_dir, _TEMPORAL_MODEL_FILENAME)
    if not os.path.exists(path):
        return None
    model = build_sequence_model(
        model_type=model_type,
        tabular_feature_dim=tabular_feature_dim,
        lstm_hidden_dim=lstm_hidden_dim,
        max_seq_len=max_seq_len,
    )
    state = torch.load(path, weights_only=True, map_location="cpu")
    model.load_state_dict(state)
    model.eval()
    logger.info("Loaded WashTradeSequenceModel from %s", path)
    return model
