"""E2E: one complete federated-learning round executes without error.

This test exercises the full ``FederatedClient`` protocol in-process:
local training → soft-label generation → delta clipping → DP noise injection.
It does not start a network server; the HTTP-exchange step is intentionally
omitted so the test remains deterministic and infrastructure-free.

Why this test lives in tests/e2e/
----------------------------------
The federated round touches multiple real subsystems in sequence
(feature engineering, model training, knowledge-distillation pipeline,
differential-privacy noise, gradient clipping), making it broader than a
unit test. It does not require a running API or external service, so it does
not use the ``e2e_client`` fixture.

Changes in this revision
------------------------
* ``_build_public_dataset`` is an internal helper (underscore-prefixed). The
  previous test called it directly, coupling the test to the private API.  The
  revised test calls ``FederatedClient.participate_in_round`` (the public
  method) with an in-process ``FederatedAggregationServer`` instead, which is
  the documented way to run a complete round without a real HTTP server.  This
  matches the pattern used in ``tests/test_federated_client.py``.
* The private training matrix previously used a hardcoded 15-column shape. The
  ``FederatedClient`` trains on whatever columns are in the data it receives;
  the public dataset is built from the real feature pipeline so the shapes are
  always consistent — no hardcoded column count required.
* ``np.random.seed`` has been replaced with a local ``np.random.default_rng``
  so the test does not mutate global NumPy RNG state, which is a hidden
  side-effect that can make other tests non-deterministic.
* Assertions are enriched with failure messages so failing output is
  immediately readable.
"""

import numpy as np
import pytest

pytestmark = pytest.mark.e2e


class TestFederatedTrainingRound:
    def test_federated_round_completes(self):
        """A single in-process federated round completes without error.

        Uses ``FederatedClient.participate_in_round`` with a fresh
        ``FederatedAggregationServer`` so that the full protocol path
        (train → soft-label → delta → clip → DP noise → submit → distil)
        is exercised through the public API rather than via private helpers.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from detection.federated.client import FederatedClient
        from detection.federated.server import FederatedAggregationServer

        # Use a local RNG so we never mutate the global NumPy random state.
        rng = np.random.default_rng(seed=42)

        private_key = Ed25519PrivateKey.generate()
        client = FederatedClient(
            operator_id="e2e-test-operator", private_key=private_key
        )

        # Build a small private dataset that matches the real feature
        # dimensionality.  The public dataset is built internally by
        # participate_in_round via _build_public_dataset (seed=0), so we only
        # need to supply the private split.
        from detection.feature_engineering import FEATURE_NAMES

        n_features = len(FEATURE_NAMES)
        X_priv = rng.standard_normal((50, n_features)).astype(np.float64)
        # Deterministic binary labels: positive when first feature > 0.
        y_priv = (X_priv[:, 0] > 0).astype(int)

        server = FederatedAggregationServer()

        noisy_soft_labels = client.participate_in_round(
            server=server,
            X_priv=X_priv,
            y_priv=y_priv,
        )

        assert noisy_soft_labels.ndim == 1, (
            f"Expected 1-D soft-label array, got shape {noisy_soft_labels.shape}"
        )
        assert noisy_soft_labels.shape[0] > 0, (
            "participate_in_round returned an empty soft-label array"
        )
        assert np.all((noisy_soft_labels >= 0.0) & (noisy_soft_labels <= 1.0)), (
            "Soft labels must be clipped to [0, 1] after DP noise injection; "
            f"range was [{noisy_soft_labels.min():.4f}, {noisy_soft_labels.max():.4f}]"
        )
