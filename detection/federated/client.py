"""Federated Learning client for LedgerLens exchange operators.

Each operator runs a FederatedClient against their private labelled dataset.
The client never sends raw transactions or model weights to the server —
only soft-label predictions on a shared public synthetic dataset (seed=0).

Knowledge Distillation protocol (Option B):
  1. Train local RF/XGB/LGBM ensemble on private data.
  2. Generate soft-label vector p_i on the shared public dataset X_pub.
  3. Compute delta_i = p_i - p_global_prev.
  4. Clip delta_i if its L2 norm exceeds GRADIENT_CLIP_THRESHOLD.
  5. Inject Gaussian DP noise onto delta_i (client-side privacy).
  6. Send noisy_soft_labels = p_global_prev + noisy_delta_i to server.
  7. Receive p_global (FedAvg of all participants' soft labels).
  8. Retrain local ensemble on private data augmented with (X_pub, round(p_global)).

Warm-starting:
  - XGBoost: xgb_model= parameter passes the booster from the previous round.
  - LightGBM: init_model= parameter passes the previous LGBMClassifier.
  - RandomForest: retrained from scratch on the combined dataset (no sklearn
    warm-start for leaf structures; gradient-approximation is not applicable
    to the KD approach which replaces the RF update with a retrain step).
"""

from __future__ import annotations

import json
import logging
import math

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from lightgbm import LGBMClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score
from xgboost import XGBClassifier

from config.settings import settings
from detection.dataset import build_training_dataset
from detection.feature_engineering import FEATURE_NAMES
from detection.federated.server import FederatedAggregationServer
from ingestion.synthetic_data import generate_synthetic_dataset

logger = logging.getLogger("ledgerlens.federated.client")

# Public dataset seed — must be identical for every participant.
_PUBLIC_DATASET_SEED = 0


def _build_public_dataset() -> np.ndarray:
    """Return feature matrix X_pub derived from the shared synthetic dataset (seed=0)."""
    trades, meta, events, labels = generate_synthetic_dataset(
        n_normal_accounts=40,
        n_wash_rings=6,
        ring_size=3,
        seed=_PUBLIC_DATASET_SEED,
    )
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
    X = df[FEATURE_NAMES].fillna(0.0).values.astype(np.float64)
    return X


def _ensemble_predict_proba(
    models: dict,
    X: np.ndarray,
    ensemble_weights: dict[str, float],
) -> np.ndarray:
    """Weighted ensemble soft-label prediction."""
    total_w = sum(ensemble_weights.get(n, 0.0) for n in models)
    if total_w <= 0:
        total_w = len(models)
    probs = np.zeros(X.shape[0], dtype=np.float64)
    for name, model in models.items():
        w = ensemble_weights.get(name, 1.0) / total_w
        probs += w * model.predict_proba(X)[:, 1]
    return probs


def _gaussian_sigma(sensitivity: float, epsilon: float, delta: float) -> float:
    if epsilon <= 0 or delta <= 0:
        return 0.0
    return sensitivity * math.sqrt(2.0 * math.log(1.25 / delta)) / epsilon


class FederatedClient:
    """Federated learning participant (exchange operator node)."""

    def __init__(
        self,
        operator_id: str,
        private_key: Ed25519PrivateKey | None = None,
        dp_epsilon: float | None = None,
        dp_delta: float | None = None,
        gradient_clip_threshold: float | None = None,
        noise_multiplier: float | None = None,
    ) -> None:
        self.operator_id = operator_id
        self._private_key = private_key or Ed25519PrivateKey.generate()
        self.dp_epsilon = dp_epsilon if dp_epsilon is not None else settings.federated_dp_epsilon
        self.dp_delta = dp_delta if dp_delta is not None else settings.federated_dp_delta
        self.gradient_clip_threshold = (
            gradient_clip_threshold
            if gradient_clip_threshold is not None
            else settings.gradient_clip_threshold
        )
        # When noise_multiplier > 0 use σ = clip_norm × nm directly (RDP path).
        # When 0 or unset, fall back to the (ε, δ)-parametrised Gaussian formula.
        self.noise_multiplier = (
            noise_multiplier if noise_multiplier is not None
            else settings.federated_noise_multiplier
        )
        self._models: dict = {}
        self._prev_xgb_booster = None
        self._prev_lgbm_model = None

    @property
    def public_key_der(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo
        )

    # ------------------------------------------------------------------
    # Local model training
    # ------------------------------------------------------------------

    def train_local_models(
        self,
        X: np.ndarray,
        y: np.ndarray,
        random_state: int = 42,
    ) -> None:
        """Train RF/XGB/LGBM on operator's private data."""
        rf = RandomForestClassifier(n_estimators=100, random_state=random_state, n_jobs=-1)
        rf.fit(X, y)

        xgb = XGBClassifier(eval_metric="logloss", random_state=random_state, verbosity=0)
        if self._prev_xgb_booster is not None:
            xgb.fit(X, y, xgb_model=self._prev_xgb_booster)
        else:
            xgb.fit(X, y)

        lgbm = LGBMClassifier(random_state=random_state, verbose=-1)
        if self._prev_lgbm_model is not None:
            lgbm.fit(X, y, init_model=self._prev_lgbm_model)
        else:
            lgbm.fit(X, y)

        self._models = {"random_forest": rf, "xgboost": xgb, "lightgbm": lgbm}
        self._prev_xgb_booster = xgb.get_booster()
        self._prev_lgbm_model = lgbm

    # ------------------------------------------------------------------
    # Knowledge Distillation helpers
    # ------------------------------------------------------------------

    def compute_soft_labels(self, X_pub: np.ndarray) -> np.ndarray:
        """Return ensemble soft-label predictions on the public dataset."""
        if not self._models:
            raise RuntimeError("No local models — call train_local_models first")
        weights = {
            "random_forest": settings.ensemble_weight_rf,
            "xgboost": settings.ensemble_weight_xgb,
            "lightgbm": settings.ensemble_weight_lgbm,
        }
        return _ensemble_predict_proba(self._models, X_pub, weights)

    def _clip_delta(self, delta: np.ndarray) -> np.ndarray:
        norm = float(np.linalg.norm(delta))
        if norm > self.gradient_clip_threshold:
            delta = delta * (self.gradient_clip_threshold / norm)
        return delta

    def split_gradient_for_smpc(
        self,
        gradient: np.ndarray,
        n_shares: int = 3,
        seed: int | None = None,
    ) -> tuple[list[np.ndarray], list[str]]:
        """Split ``gradient`` into additive SMPC shares and return (shares, commitments).

        Returns a list of ``n_shares`` gradient arrays whose sum equals the
        original gradient, plus a parallel list of SHA-256 commitment strings
        that aggregators can use to verify share inclusion.
        """
        from detection.federated.smpc import commit_share, split_gradient
        shares = split_gradient(gradient, n_shares=n_shares, seed=seed)
        commitments = [commit_share(s) for s in shares]
        return shares, commitments

    def inject_dp_noise(self, delta: np.ndarray) -> np.ndarray:
        """Add Gaussian DP noise to `delta` (client-side privacy guarantee)."""
        if self.noise_multiplier > 0.0:
            sigma = self.gradient_clip_threshold * self.noise_multiplier
        else:
            sigma = _gaussian_sigma(self.gradient_clip_threshold, self.dp_epsilon, self.dp_delta)
        noise = np.random.normal(0.0, sigma, delta.shape)
        return delta + noise

    def _sign_payload(
        self,
        noisy_soft_labels: np.ndarray,
        n_samples: int,
        round_id: str,
    ) -> bytes:
        payload = json.dumps(
            {
                "participant_id": self.operator_id,
                "round_id": round_id,
                "soft_labels": noisy_soft_labels.tolist(),
                "n_samples": n_samples,
            },
            sort_keys=True,
        ).encode()
        return self._private_key.sign(payload)

    # ------------------------------------------------------------------
    # Fine-tuning with distilled labels
    # ------------------------------------------------------------------

    def update_with_distilled_labels(
        self,
        X_priv: np.ndarray,
        y_priv: np.ndarray,
        X_pub: np.ndarray,
        global_soft_labels: np.ndarray,
        random_state: int = 42,
    ) -> None:
        """Retrain local ensemble augmented with the server's distilled labels."""
        y_distill = (global_soft_labels >= 0.5).astype(int)
        X_aug = np.vstack([X_priv, X_pub])
        y_aug = np.concatenate([y_priv, y_distill])

        rf = RandomForestClassifier(n_estimators=100, random_state=random_state, n_jobs=-1)
        rf.fit(X_aug, y_aug)

        xgb = XGBClassifier(eval_metric="logloss", random_state=random_state, verbosity=0)
        if self._prev_xgb_booster is not None:
            xgb.fit(X_aug, y_aug, xgb_model=self._prev_xgb_booster)
        else:
            xgb.fit(X_aug, y_aug)

        lgbm = LGBMClassifier(random_state=random_state, verbose=-1)
        if self._prev_lgbm_model is not None:
            lgbm.fit(X_aug, y_aug, init_model=self._prev_lgbm_model)
        else:
            lgbm.fit(X_aug, y_aug)

        self._models = {"random_forest": rf, "xgboost": xgb, "lightgbm": lgbm}
        self._prev_xgb_booster = xgb.get_booster()
        self._prev_lgbm_model = lgbm

    # ------------------------------------------------------------------
    # Main round participation (in-process mode)
    # ------------------------------------------------------------------

    def participate_in_round(
        self,
        server: FederatedAggregationServer,
        X_priv: np.ndarray,
        y_priv: np.ndarray,
        X_pub: np.ndarray | None = None,
        random_state: int = 42,
    ) -> np.ndarray:
        """Execute one federated round against an in-process server.

        Returns the updated local soft labels after distillation.
        """
        if X_pub is None:
            X_pub = _build_public_dataset()

        # 1. Train on private data
        self.train_local_models(X_priv, y_priv, random_state=random_state)

        # 2. Compute soft labels on public dataset
        soft_labels = self.compute_soft_labels(X_pub)

        # 3. Compute delta
        prev_global = server.get_global_soft_labels()
        if prev_global is None:
            prev_global = np.full(len(soft_labels), 0.5)
        delta = soft_labels - prev_global

        # 4. Clip
        delta = self._clip_delta(delta)

        # 5. Client-side DP noise
        noisy_delta = self.inject_dp_noise(delta)
        noisy_soft_labels = np.clip(prev_global + noisy_delta, 0.0, 1.0)

        # 6. Sign and send
        n_samples = len(y_priv)
        signature = self._sign_payload(noisy_soft_labels, n_samples, server.get_round_id())
        server.submit_update(
            participant_id=self.operator_id,
            soft_labels=noisy_soft_labels,
            n_samples=n_samples,
            signature=signature,
        )

        # 7. Fine-tune with distilled labels (uses global from server after aggregation)
        global_labels = server.get_global_soft_labels()
        if global_labels is not None:
            self.update_with_distilled_labels(X_priv, y_priv, X_pub, global_labels, random_state)

        return noisy_soft_labels

    # ------------------------------------------------------------------
    # Evaluation helper
    # ------------------------------------------------------------------

    def evaluate(self, X_test: np.ndarray, y_test: np.ndarray) -> float:
        """Return ensemble AUC-ROC on a held-out test set."""
        weights = {
            "random_forest": settings.ensemble_weight_rf,
            "xgboost": settings.ensemble_weight_xgb,
            "lightgbm": settings.ensemble_weight_lgbm,
        }
        probs = _ensemble_predict_proba(self._models, X_test, weights)
        return float(roc_auc_score(y_test, probs))
