"""LSTM Temporal Anomaly Model definition, training, and prediction.

Defines the TemporalAnomalyLSTM network architecture, training loop, and inference utilities.
"""

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset


class TemporalAnomalyLSTM(nn.Module):
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

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (batch, seq_len, features) → (batch, 1) risk probability"""
        lstm_out, _ = self.lstm(x)
        # Take the output of the last time step
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

    for epoch in range(epochs):
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

    # If sequence is empty or invalid, return 0.0
    if sequence.shape[0] == 0 or sequence.shape[1] == 0:
        return 0.0

    with torch.no_grad():
        x = torch.tensor(sequence, dtype=torch.float32)
        prob = model(x).item()
    return prob
