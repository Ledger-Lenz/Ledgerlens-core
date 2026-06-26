"""PyTorch MLP classifier for federated learning (Issue #145).

Separate from the primary RF/XGBoost/LightGBM ensemble used in non-FL scoring.
Compatible with Opacus PrivacyEngine for DP-SGD training.
"""

from __future__ import annotations

from typing import List

import torch
import torch.nn as nn


class WashTradeMLPClassifier(nn.Module):
    """Lightweight MLP for FL-specific binary wash-trade classification.

    Designed to be compatible with Opacus PrivacyEngine for DP-SGD.
    Input dimension matches LedgerLens's 35-feature schema.
    """

    def __init__(
        self,
        input_dim: int = 35,
        hidden_dims: List[int] = None,
        dropout: float = 0.3,
    ):
        super().__init__()
        if hidden_dims is None:
            hidden_dims = [128, 64, 32]
        layers: list[nn.Module] = []
        prev = input_dim
        for h in hidden_dims:
            layers += [nn.Linear(prev, h), nn.ReLU(), nn.Dropout(dropout)]
            prev = h
        layers.append(nn.Linear(prev, 1))  # binary logits
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)  # logits, shape (batch,)
