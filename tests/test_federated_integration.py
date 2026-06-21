"""Integration test: 3 simulated participants + 1 server, 2 federated rounds.

Asserts that after 2 rounds of Knowledge-Distillation FedAvg the global
model's AUC-ROC on held-out synthetic data is ≥ 0.75.

Runs entirely in-process using threads — no HTTP server required.
"""

import threading

import numpy as np

from detection.dataset import build_training_dataset
from detection.feature_engineering import FEATURE_NAMES
from detection.federated.client import FederatedClient, _build_public_dataset
from detection.federated.server import FederatedAggregationServer
from ingestion.synthetic_data import generate_synthetic_dataset


def _make_data(seed: int) -> tuple[np.ndarray, np.ndarray]:
    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=40,
        n_wash_rings=8,
        ring_size=3,
        seed=seed,
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
    X = df[FEATURE_NAMES].fillna(0.0).values.astype(np.float64)
    y = df["label"].values.astype(int)
    return X, y


def _register_client(
    server: FederatedAggregationServer, client: FederatedClient
) -> None:
    pub_der = client.public_key_der
    server.register_participant(client.operator_id, pub_der)


def test_federated_integration_two_rounds(tmp_path):
    db = str(tmp_path / "audit.db")
    server = FederatedAggregationServer(
        min_participants=3,
        gradient_clip_threshold=100.0,
        gradient_outlier_threshold=-2.0,  # disable outlier detection for integration
        dp_epsilon=2.0,
        dp_delta=1e-5,
        dp_max_epsilon=1000.0,
        db_path=db,
    )

    # Three participants each with their own private data slice
    participants: list[tuple[FederatedClient, np.ndarray, np.ndarray]] = []
    for i in range(3):
        X, y = _make_data(seed=i + 10)
        client = FederatedClient(
            operator_id=f"operator-{i}",
            dp_epsilon=2.0,
            dp_delta=1e-5,
            gradient_clip_threshold=100.0,
        )
        _register_client(server, client)
        participants.append((client, X, y))

    # Held-out evaluation set (separate from all participants' private data)
    X_eval, y_eval = _make_data(seed=99)

    # Shared public dataset (same for all participants)
    X_pub = _build_public_dataset()

    def run_participant_round(client, X, y, barrier, round_errors):
        try:
            client.participate_in_round(server, X, y, X_pub=X_pub, random_state=42)
        except Exception as exc:
            round_errors.append(str(exc))
        finally:
            barrier.wait()

    for round_num in range(2):
        errors: list[str] = []
        barrier = threading.Barrier(len(participants))
        threads = [
            threading.Thread(
                target=run_participant_round,
                args=(client, X, y, barrier, errors),
            )
            for client, X, y in participants
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert not errors, f"Round {round_num + 1} errors: {errors}"

    # Evaluate the ensemble of the first participant on held-out data
    # (all participants converge to similar performance after distillation)
    first_client, _, _ = participants[0]
    auc = first_client.evaluate(X_eval, y_eval)
    assert auc >= 0.75, (
        f"Expected AUC-ROC ≥ 0.75 after 2 federated rounds, got {auc:.4f}"
    )

    # Confirm audit records were written for both rounds
    from detection.federated.audit import get_audit_records
    records = get_audit_records(db_path=db)
    assert len(records) >= 2, (
        f"Expected ≥ 2 audit records (one per round), got {len(records)}"
    )
