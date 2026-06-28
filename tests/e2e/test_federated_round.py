"""E2E: federated training round completes without error (using mock exchange client)."""


import numpy as np
import pytest

pytestmark = pytest.mark.e2e


class TestFederatedTrainingRound:
    def test_federated_round_completes(self):
        """A single federated round with mocked HTTP client completes without error."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from detection.federated.client import FederatedClient, _build_public_dataset

        private_key = Ed25519PrivateKey.generate()
        client = FederatedClient(operator_id="e2e-test-operator", private_key=private_key)

        np.random.seed(42)
        X = np.random.randn(50, 15).astype(np.float64)
        y = (X[:, 0] > 0).astype(int)

        client.train_local_models(X, y)

        X_pub = _build_public_dataset()
        soft_labels = client.compute_soft_labels(X_pub)

        assert soft_labels.shape[0] == X_pub.shape[0]
        assert np.all((soft_labels >= 0.0) & (soft_labels <= 1.0))

        prev_global = np.full(len(X_pub), 0.5)
        delta = soft_labels - prev_global
        delta = client._clip_delta(delta)
        noisy_delta = client.inject_dp_noise(delta)
        noisy_soft_labels = np.clip(prev_global + noisy_delta, 0.0, 1.0)

        assert noisy_soft_labels.shape == soft_labels.shape
