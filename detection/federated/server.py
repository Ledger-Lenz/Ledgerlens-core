"""Federated Aggregation Server — Knowledge Distillation FedAvg for LedgerLens.

Design rationale (Option B — Knowledge Distillation):
  Tree ensembles (RF, XGB, LGBM) have no gradient tensors in the neural-network
  sense.  Rather than serialising leaf-value arrays (Option A, XGB/LGBM only) or
  adding an MLP head (Option C), we use knowledge distillation:

  1. A shared public unlabelled dataset is derived from synthetic trades
     (seed=0) and is identical for every participant.
  2. Each participant runs their *private* ensemble on the public dataset and
     sends the resulting soft-label vector  p_i ∈ [0,1]^N  to the server.
  3. The server selects this round's Krum/Multi-Krum survivors (peer-distance
     outlier exclusion; see `_select_krum_survivors`), then computes the
     weighted FedAvg of soft labels over the survivors:
         p_global = Σ (n_i / N_total) × p_i
  4. The "gradient" used for norm-clipping, cosine-outlier detection, and
     Krum peer-distance comparison is
         delta_i = p_i - p_global_prev  (difference from the previous round).
  5. Participants receive p_global and retrain their local ensembles using
     the public dataset annotated with the distilled labels as an augmentation
     source (see client.py).

Privacy properties:
  - No raw transaction data or model weights leave any participant.
  - Soft labels on a *public* synthetic dataset carry very limited information
    about private training distributions.
  - The server additionally clips and noises each update before aggregation
    (ε, δ)-DP Gaussian mechanism as a defence-in-depth layer.

Run as a standalone process via:
    python -m cli federated server
"""

from __future__ import annotations

import base64
import json
import logging
import math
import threading
import uuid
from dataclasses import dataclass

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
)
from cryptography.exceptions import InvalidSignature

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from dp_accounting import dp_event as _dp_event
from dp_accounting.rdp import rdp_privacy_accountant as _rdp_pa

from api.auth import require_admin_key
from config.settings import settings
from detection.federated.admission import (
    AdmissionError,
    admit_participant as _admit_participant,
    get_admission,
)
from detection.federated.audit import (
    build_record,
    get_cumulative_epsilon,
    get_round_count,
    save_audit_record,
    sign_record,
)
from detection.federated.krum import KrumAggregator
from detection.federated.weighting import apply_weight_share_cap

logger = logging.getLogger("ledgerlens.federated.server")


@dataclass
class _ParticipantUpdate:
    participant_id: str
    noisy_soft_labels: np.ndarray
    delta: np.ndarray
    n_samples: int
    claimed_n_samples: int
    excluded: bool = False
    exclusion_reason: str = ""


@dataclass
class _Participant:
    participant_id: str
    public_key: Ed25519PublicKey
    # Admission-approved ceiling on claimed n_samples; math.inf when admission
    # control is disabled (federated_admission_required=False).
    max_n_samples: float
    n_samples_last_round: int = 0
    # Largest *accepted* (non-excluded) effective n_samples this participant
    # has ever claimed, used by the cross-round consistency check. 0 means
    # "no accepted round yet" -- the check is skipped for a participant's
    # first round since there is no history to compare against.
    n_samples_history_max: int = 0


class FederatedAggregationServer:
    """In-process federated aggregation server.

    The FastAPI HTTP layer (at the bottom of this file) wraps this class.
    Tests can instantiate it directly and call its methods without HTTP.
    """

    def __init__(
        self,
        min_participants: int | None = None,
        gradient_clip_threshold: float | None = None,
        gradient_outlier_threshold: float | None = None,
        dp_epsilon: float | None = None,
        dp_delta: float | None = None,
        dp_max_epsilon: float | None = None,
        db_path: str | None = None,
        server_private_key: Ed25519PrivateKey | None = None,
        noise_multiplier: float | None = None,
        target_delta: float | None = None,
        admission_required: bool | None = None,
        max_participant_weight_fraction: float | None = None,
        max_n_samples_growth_factor: float | None = None,
        use_krum: bool | None = None,
    ) -> None:
        self.min_participants = min_participants if min_participants is not None else settings.federated_min_participants
        self.gradient_clip_threshold = gradient_clip_threshold if gradient_clip_threshold is not None else settings.gradient_clip_threshold
        self.gradient_outlier_threshold = gradient_outlier_threshold if gradient_outlier_threshold is not None else settings.gradient_outlier_threshold
        self.dp_epsilon = dp_epsilon if dp_epsilon is not None else settings.federated_dp_epsilon
        self.dp_delta = dp_delta if dp_delta is not None else settings.federated_dp_delta
        self.dp_max_epsilon = dp_max_epsilon if dp_max_epsilon is not None else settings.federated_dp_max_epsilon
        self.db_path = db_path
        # Sybil / weight-inflation defenses (see detection/federated/admission.py
        # and detection/federated/weighting.py for the full threat model).
        self.admission_required = (
            admission_required if admission_required is not None
            else settings.federated_admission_required
        )
        self.max_participant_weight_fraction = (
            max_participant_weight_fraction if max_participant_weight_fraction is not None
            else settings.federated_max_participant_weight_fraction
        )
        self.max_n_samples_growth_factor = (
            max_n_samples_growth_factor if max_n_samples_growth_factor is not None
            else settings.federated_max_n_samples_growth_factor
        )
        # Per-round Krum/Multi-Krum peer-distance defense; see
        # _select_krum_survivors for how f/m are derived from the live
        # participant count each round rather than a static config value.
        self.use_krum = use_krum if use_krum is not None else settings.federated_use_krum
        # noise_multiplier > 0 enables the RDP accounting path (σ = clip_norm × nm).
        # 0.0 keeps the legacy linear ε-accumulation for backward compatibility.
        self.noise_multiplier = (
            noise_multiplier if noise_multiplier is not None
            else settings.federated_noise_multiplier
        )
        self.target_delta = target_delta if target_delta is not None else self.dp_delta

        self._lock = threading.Lock()
        self._participants: dict[str, _Participant] = {}
        self._pending_updates: dict[str, _ParticipantUpdate] = {}
        self._global_soft_labels: np.ndarray | None = None
        self._previous_mean_delta: np.ndarray | None = None
        self._current_round_id: str = str(uuid.uuid4())
        self._round_number: int = get_round_count(db_path)
        self._cumulative_epsilon: float = get_cumulative_epsilon(db_path)

        # Reconstruct RDP accountant state from the persisted round count so that
        # ε projections remain accurate across server restarts.
        if self.noise_multiplier > 0.0 and self._round_number > 0:
            acc = _rdp_pa.RdpAccountant()
            acc.compose(
                _dp_event.SelfComposedDpEvent(
                    _dp_event.GaussianDpEvent(noise_multiplier=self.noise_multiplier),
                    count=self._round_number,
                )
            )
            self._cumulative_epsilon = acc.get_epsilon(target_delta=self.target_delta)

        if server_private_key is None:
            server_private_key = Ed25519PrivateKey.generate()
        self._private_key = server_private_key
        self._public_key = self._private_key.public_key()

    # ------------------------------------------------------------------
    # Participant registration
    # ------------------------------------------------------------------

    def register_participant(self, participant_id: str, public_key_der: bytes) -> None:
        """Register an operator's Ed25519 public key.

        Requires `participant_id` to have been admitted by an operator first
        (see :func:`detection.federated.admission.admit_participant`) unless
        `admission_required` is False. Admission assigns the ceiling that
        bounds this participant's claimed `n_samples` in every future round
        (see `submit_update`) -- registration without admission would let
        any actor mint an unlimited number of participant identities with no
        cap on the aggregation weight each can claim.

        Raises AdmissionError if admission is required and this
        participant_id has not been admitted (or was revoked).
        """
        pub = load_der_public_key(public_key_der)
        if not isinstance(pub, Ed25519PublicKey):
            raise ValueError("Expected Ed25519 public key")

        if self.admission_required:
            record = get_admission(participant_id, self.db_path)
            if record is None or record.revoked:
                raise AdmissionError(
                    f"Participant {participant_id!r} is not admitted. An operator "
                    "must call FederatedAggregationServer.admit_participant() (or "
                    "`cli.py federated admit` / POST /federated/admit) before this "
                    "identity may register."
                )
            max_n_samples: float = record.max_n_samples
        else:
            # Admission disabled: explicitly insecure (see settings docstring for
            # federated_admission_required) -- no ceiling is enforced.
            max_n_samples = math.inf

        with self._lock:
            self._participants[participant_id] = _Participant(
                participant_id=participant_id,
                public_key=pub,
                max_n_samples=max_n_samples,
            )
        logger.info(
            "Registered participant %s (max_n_samples=%s)",
            participant_id,
            "unbounded" if math.isinf(max_n_samples) else int(max_n_samples),
        )

    def admit_participant(
        self, participant_id: str, max_n_samples: int, admitted_by: str = "operator"
    ):
        """Convenience wrapper: admit a participant using this server's db_path.

        Equivalent to calling `detection.federated.admission.admit_participant`
        directly; provided so callers that already hold a server instance
        don't need a second import. Must be called before `register_participant`
        for this `participant_id` when `admission_required` is True.
        """
        return _admit_participant(participant_id, max_n_samples, admitted_by, db_path=self.db_path)

    # ------------------------------------------------------------------
    # Round management
    # ------------------------------------------------------------------

    def get_global_soft_labels(self) -> np.ndarray | None:
        """Return current global soft labels (None before first aggregation)."""
        return self._global_soft_labels

    def get_round_id(self) -> str:
        return self._current_round_id

    def get_server_public_key_der(self) -> bytes:
        return self._public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)

    # ------------------------------------------------------------------
    # RDP budget helpers
    # ------------------------------------------------------------------

    def _epsilon_at_round(self, n: int) -> float:
        """Return RDP-computed ε after `n` rounds of the Gaussian mechanism."""
        if n <= 0:
            return 0.0
        acc = _rdp_pa.RdpAccountant()
        acc.compose(
            _dp_event.SelfComposedDpEvent(
                _dp_event.GaussianDpEvent(noise_multiplier=self.noise_multiplier),
                count=n,
            )
        )
        return acc.get_epsilon(target_delta=self.target_delta)

    # ------------------------------------------------------------------
    # Update submission
    # ------------------------------------------------------------------

    def submit_update(
        self,
        participant_id: str,
        soft_labels: np.ndarray,
        n_samples: int,
        signature: bytes,
    ) -> dict:
        """Accept a gradient update from a participant.

        Returns a status dict with keys: accepted, reason.
        Raises ValueError if the participant is not registered or the
        privacy budget is exhausted.
        """
        with self._lock:
            if participant_id not in self._participants:
                raise ValueError(f"Unknown participant: {participant_id}")

            if self.noise_multiplier > 0.0:
                projected_epsilon = self._epsilon_at_round(self._round_number + 1)
                if projected_epsilon > self.dp_max_epsilon:
                    raise RuntimeError(
                        f"Privacy budget exhausted: projected ε={projected_epsilon:.4f} "
                        f"after next round would exceed max ε={self.dp_max_epsilon:.4f}. "
                        "Operator acknowledgement required."
                    )
            elif self._cumulative_epsilon >= self.dp_max_epsilon:
                raise RuntimeError(
                    f"Privacy budget exhausted: cumulative ε={self._cumulative_epsilon:.4f} "
                    f">= max ε={self.dp_max_epsilon:.4f}. Operator acknowledgement required."
                )

            # Authenticate the update
            participant = self._participants[participant_id]
            payload = self._build_update_payload(participant_id, soft_labels, n_samples)
            try:
                participant.public_key.verify(signature, payload)
            except InvalidSignature:
                raise ValueError(f"Invalid signature from participant {participant_id}")

            # Compute delta relative to previous global
            prev = self._global_soft_labels
            if prev is None:
                prev = np.full_like(soft_labels, 0.5)
            delta = soft_labels - prev

            # Norm clipping
            delta_norm = float(np.linalg.norm(delta))
            if delta_norm > self.gradient_clip_threshold:
                hashed_id = __import__("hashlib").sha256(participant_id.encode()).hexdigest()[:16]
                logger.warning(
                    "Gradient norm %.4f exceeds clip threshold %.4f for participant %s — clipping",
                    delta_norm,
                    self.gradient_clip_threshold,
                    hashed_id,
                )
                delta = delta * (self.gradient_clip_threshold / delta_norm)

            # Cosine similarity outlier detection
            excluded = False
            exclusion_reason = ""
            if self._previous_mean_delta is not None:
                mean_norm = float(np.linalg.norm(self._previous_mean_delta))
                cur_norm = float(np.linalg.norm(delta))
                if mean_norm > 1e-10 and cur_norm > 1e-10:
                    cosine_sim = float(
                        np.dot(delta, self._previous_mean_delta) / (cur_norm * mean_norm)
                    )
                    if cosine_sim < self.gradient_outlier_threshold:
                        hashed_id = __import__("hashlib").sha256(participant_id.encode()).hexdigest()[:16]
                        logger.warning(
                            "Cosine similarity %.4f < threshold %.4f for participant %s — "
                            "flagging as potential gradient poisoning attempt",
                            cosine_sim,
                            self.gradient_outlier_threshold,
                            hashed_id,
                        )
                        excluded = True
                        exclusion_reason = f"cosine_sim={cosine_sim:.4f} < threshold"

            # n_samples ceiling: clamp the claimed value at this participant's
            # admission-approved ceiling. This is what actually stops a single
            # registered identity from claiming an arbitrarily large aggregation
            # weight -- unlike norm-clip/cosine-similarity above, which bound the
            # *gradient's* magnitude/direction and say nothing about weight.
            claimed_n_samples = n_samples
            effective_n_samples = min(n_samples, participant.max_n_samples)
            if effective_n_samples < claimed_n_samples:
                hashed_id = __import__("hashlib").sha256(participant_id.encode()).hexdigest()[:16]
                logger.warning(
                    "Participant %s claimed n_samples=%d exceeding its admitted "
                    "ceiling of %d — clamping to the ceiling for aggregation weight",
                    hashed_id,
                    claimed_n_samples,
                    int(participant.max_n_samples),
                )
            effective_n_samples = int(effective_n_samples)

            # Cross-round consistency: flag a sudden large jump in a participant's
            # claimed n_samples relative to its own accepted history -- catches an
            # already-admitted identity (with a legitimately large ceiling) that
            # starts claiming far more than its established pattern, e.g. because
            # it was compromised. Skipped on a participant's first accepted round
            # (n_samples_history_max == 0), since there is no history yet.
            if not excluded and participant.n_samples_history_max > 0:
                growth_limit = self.max_n_samples_growth_factor * participant.n_samples_history_max
                if effective_n_samples > growth_limit:
                    hashed_id = __import__("hashlib").sha256(participant_id.encode()).hexdigest()[:16]
                    logger.warning(
                        "Participant %s claimed n_samples=%d, more than %.1fx its "
                        "historical accepted max of %d — flagging as a possible "
                        "weight-inflation attempt",
                        hashed_id,
                        effective_n_samples,
                        self.max_n_samples_growth_factor,
                        participant.n_samples_history_max,
                    )
                    excluded = True
                    growth_reason = (
                        f"n_samples={effective_n_samples} > "
                        f"{self.max_n_samples_growth_factor}x historical max "
                        f"{participant.n_samples_history_max}"
                    )
                    exclusion_reason = (
                        f"{exclusion_reason}; {growth_reason}" if exclusion_reason else growth_reason
                    )

            # Reconstruct soft labels from the (possibly clipped) delta so the
            # aggregation step always operates on norm-bounded updates.
            effective_soft_labels = np.clip(prev + delta, 0.0, 1.0)

            update = _ParticipantUpdate(
                participant_id=participant_id,
                noisy_soft_labels=effective_soft_labels,
                delta=delta,
                n_samples=effective_n_samples,
                claimed_n_samples=claimed_n_samples,
                excluded=excluded,
                exclusion_reason=exclusion_reason,
            )
            self._pending_updates[participant_id] = update
            participant.n_samples_last_round = effective_n_samples
            if not excluded:
                participant.n_samples_history_max = max(
                    participant.n_samples_history_max, effective_n_samples
                )

            n_valid = sum(1 for u in self._pending_updates.values() if not u.excluded)
            status = {
                "accepted": not excluded,
                "reason": exclusion_reason if excluded else "ok",
                "pending_valid": n_valid,
                "quorum": self.min_participants,
                "n_samples_effective": effective_n_samples,
                "n_samples_clamped": effective_n_samples < claimed_n_samples,
            }

            # Auto-aggregate when quorum is reached
            if n_valid >= self.min_participants and not self._aggregation_in_progress:
                self._aggregation_in_progress = True
                self._aggregate_locked()
                self._aggregation_in_progress = False

            return status

    _aggregation_in_progress: bool = False

    def _build_update_payload(
        self, participant_id: str, soft_labels: np.ndarray, n_samples: int
    ) -> bytes:
        return json.dumps(
            {
                "participant_id": participant_id,
                "round_id": self._current_round_id,
                "soft_labels": soft_labels.tolist(),
                "n_samples": n_samples,
            },
            sort_keys=True,
        ).encode()

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def force_aggregate(self) -> np.ndarray | None:
        """Aggregate any pending valid updates immediately (for testing)."""
        with self._lock:
            return self._aggregate_locked()

    def _select_krum_survivors(
        self, valid_updates: list[_ParticipantUpdate]
    ) -> tuple[list[_ParticipantUpdate], list[str]]:
        """Exclude the most peer-distant updates via Krum/Multi-Krum, on top of
        (not instead of) the historical-cosine heuristic already applied in
        `submit_update`.

        Ordering rationale (see docs/byzantine_resilience.md): clipping and the
        cosine-heuristic exclusion already happened per-update in
        `submit_update`, *before* this runs, so Krum computes peer distances
        over already-norm-bounded deltas -- an unclipped malicious delta could
        otherwise distort every pairwise distance in the round. Krum selection
        itself runs *before* the server-side DP noise added later in
        `_aggregate_locked`: noise is only ever added to the single released
        aggregate (unchanged from the pre-Krum design), so this wiring adds no
        new privacy-budget surface -- Krum's data-dependent selection is
        reflected only in the (already-audited) list of excluded participant
        ids, not in any additional noised quantity. Adding DP noise *before*
        Krum instead would let noise push an honest update's measured distance
        closer to a malicious one, weakening the Byzantine-robustness
        guarantee, so noise must come last.

        Unlike the cosine heuristic (which compares each round's aggregate
        direction to a *historical rolling mean* and is skipped entirely on
        the first round), Krum compares updates against their *same-round*
        peers, so it runs from round 1 onward -- closing the "boiling frog"
        gap where a colluding coalition gradually drags the historical
        baseline over successive rounds.

        `f` (Byzantine tolerance) and the `2f + 2 < n` safety margin are
        derived from `n = len(valid_updates)`, the actual live count of valid
        updates *this round* -- never a static config value -- so the
        guarantee genuinely holds for the round being aggregated even as
        participants join, drop out, or get excluded by the cosine heuristic.

        Falls back to plain FedAvg over all of `valid_updates` (with a loud
        warning, per the documented fallback policy) when there are too few
        live participants this round for Krum to offer any tolerance at all.

        Returns:
            (survivors, krum_excluded_participant_ids)
        """
        n = len(valid_updates)
        if not self.use_krum:
            return valid_updates, []
        if n < 3:
            if n > 0:
                logger.warning(
                    "Krum skipped this round: only %d valid update(s), need >= 3 "
                    "for any Byzantine tolerance -- falling back to plain FedAvg "
                    "with no per-round peer-distance defense",
                    n,
                )
            return valid_updates, []

        f = n // 3
        while f > 0 and 2 * f + 2 >= n:
            f -= 1
        if 2 * f + 2 >= n:
            logger.warning(
                "Krum skipped this round: n=%d too small to satisfy 2f+2 < n "
                "even at f=0 -- falling back to plain FedAvg with no per-round "
                "peer-distance defense",
                n,
            )
            return valid_updates, []

        m = n - f
        deltas = [u.delta for u in valid_updates]
        selected, excluded, scores = KrumAggregator(f=f).select(deltas, m=m)

        survivors = [valid_updates[i] for i in selected]
        excluded_ids = [valid_updates[i].participant_id for i in excluded]
        if excluded_ids:
            hashed_ids = [
                __import__("hashlib").sha256(pid.encode()).hexdigest()[:16]
                for pid in excluded_ids
            ]
            logger.warning(
                "Krum round: excluded %d/%d update(s) as same-round peer-distance "
                "outliers (f=%d, m=%d): %s",
                len(excluded_ids), n, f, m, hashed_ids,
            )

        try:
            from detection.storage import log_krum_aggregation
            log_krum_aggregation(
                round_number=self._round_number + 1,
                n_clients=n,
                f_tolerance=f,
                m_selected=m,
                selected_indices=selected,
                excluded_indices=excluded,
                krum_scores=scores.tolist(),
                db_path=self.db_path,
            )
        except Exception:
            logger.debug("Could not persist Krum aggregation log", exc_info=True)

        return survivors, excluded_ids

    def _aggregate_locked(self) -> np.ndarray | None:
        """Must be called while holding self._lock."""
        valid_updates = [u for u in self._pending_updates.values() if not u.excluded]
        if not valid_updates:
            return None

        valid_updates, krum_excluded_ids = self._select_krum_survivors(valid_updates)
        if not valid_updates:
            return None

        n_total = sum(u.n_samples for u in valid_updates)
        if n_total == 0:
            return None

        # Weighted FedAvg on soft labels, with a per-round cap on any single
        # participant's weight share (defense-in-depth on top of the
        # admission-ceiling clamp already applied to each u.n_samples in
        # submit_update -- see detection/federated/weighting.py).
        raw_weights = np.array([u.n_samples for u in valid_updates], dtype=float)
        weights = apply_weight_share_cap(raw_weights, self.max_participant_weight_fraction)
        raw_fractions = raw_weights / raw_weights.sum()
        capped_participant_ids = [
            u.participant_id
            for u, w, raw_w in zip(valid_updates, weights, raw_fractions)
            if w < raw_w - 1e-9
        ]
        if capped_participant_ids:
            logger.warning(
                "Weight-share cap (%.2f) applied to %d participant(s) this round",
                self.max_participant_weight_fraction,
                len(capped_participant_ids),
            )

        agg = np.zeros_like(valid_updates[0].noisy_soft_labels, dtype=float)
        for u, weight in zip(valid_updates, weights):
            agg += weight * u.noisy_soft_labels

        # Server-side DP noise (defence-in-depth).
        # When noise_multiplier > 0, use σ = clip_norm × nm; else use (ε,δ) formula.
        if self.noise_multiplier > 0.0:
            sigma = self.gradient_clip_threshold * self.noise_multiplier
        else:
            sigma = self._gaussian_sigma(self.gradient_clip_threshold)
        server_noise = np.random.normal(0.0, sigma, agg.shape)
        agg = np.clip(agg + server_noise, 0.0, 1.0)

        # Norm of the aggregated update (for audit and poisoning detection)
        prev = self._global_soft_labels if self._global_soft_labels is not None else np.full_like(agg, 0.5)
        agg_delta = agg - prev
        agg_norm = float(np.linalg.norm(agg_delta))

        # Update running mean delta for cosine outlier detection next round
        if valid_updates:
            mean_delta = np.mean([u.delta for u in valid_updates], axis=0)
            self._previous_mean_delta = mean_delta

        self._global_soft_labels = agg
        self._round_number += 1

        # Update cumulative ε via RDP accountant (tight bound) or legacy linear sum.
        if self.noise_multiplier > 0.0:
            self._cumulative_epsilon = self._epsilon_at_round(self._round_number)
            dp_epsilon_consumed = self._cumulative_epsilon - (
                self._epsilon_at_round(self._round_number - 1)
            )
        else:
            self._cumulative_epsilon += self.dp_epsilon
            dp_epsilon_consumed = self.dp_epsilon

        # Audit record
        accepted_ids = [u.participant_id for u in valid_updates]
        excluded_ids = [
            u.participant_id for u in self._pending_updates.values() if u.excluded
        ] + krum_excluded_ids
        record = build_record(
            round_id=self._current_round_id,
            participant_ids=accepted_ids,
            aggregated_update_norm=agg_norm,
            dp_epsilon_consumed=dp_epsilon_consumed,
            cumulative_epsilon=self._cumulative_epsilon,
            excluded_participant_ids=excluded_ids,
            dp_delta=self.target_delta,
            noise_multiplier=self.noise_multiplier,
            weight_capped_participant_ids=capped_participant_ids,
        )
        sig = sign_record(record, self._private_key)
        save_audit_record(record, sig, self.db_path)

        logger.info(
            "Round %d aggregated: %d participants, norm=%.4f, cumulative_ε=%.4f",
            self._round_number,
            len(valid_updates),
            agg_norm,
            self._cumulative_epsilon,
        )

        # Advance round
        self._current_round_id = str(uuid.uuid4())
        self._pending_updates = {}

        if self.noise_multiplier > 0.0:
            next_projected = self._epsilon_at_round(self._round_number + 1)
            if next_projected > self.dp_max_epsilon:
                logger.warning(
                    "Privacy budget will be exhausted after round %d: "
                    "next projected ε=%.4f > max ε=%.4f",
                    self._round_number,
                    next_projected,
                    self.dp_max_epsilon,
                )
        elif self._cumulative_epsilon >= self.dp_max_epsilon:
            logger.warning(
                "Privacy budget exhausted after round %d: cumulative ε=%.4f",
                self._round_number,
                self._cumulative_epsilon,
            )

        return self._global_soft_labels

    def _gaussian_sigma(self, sensitivity: float) -> float:
        """Gaussian mechanism noise scale for (ε, δ)-DP."""
        if self.dp_epsilon <= 0 or self.dp_delta <= 0:
            return 0.0
        return sensitivity * math.sqrt(2.0 * math.log(1.25 / self.dp_delta)) / self.dp_epsilon


# ---------------------------------------------------------------------------
# FastAPI HTTP wrapper
# ---------------------------------------------------------------------------

_server_instance: FederatedAggregationServer | None = None


def get_server() -> FederatedAggregationServer:
    global _server_instance
    if _server_instance is None:
        _server_instance = FederatedAggregationServer()
    return _server_instance


federated_app = FastAPI(title="LedgerLens Federated Server")


class RegisterRequest(BaseModel):
    participant_id: str
    public_key_der_b64: str


class UpdateRequest(BaseModel):
    participant_id: str
    soft_labels_b64: str
    n_samples: int
    signature_b64: str


class AdmitParticipantRequest(BaseModel):
    participant_id: str
    max_n_samples: int
    admitted_by: str = "operator"


@federated_app.post("/federated/register")
def http_register(req: RegisterRequest) -> dict:
    pub_der = base64.b64decode(req.public_key_der_b64)
    try:
        get_server().register_participant(req.participant_id, pub_der)
    except AdmissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "registered"}


@federated_app.post("/federated/admit")
def http_admit_participant(
    req: AdmitParticipantRequest, _auth: None = Depends(require_admin_key)
) -> dict:
    """Operator-only: authorize a participant_id to register, with a ceiling
    on the n_samples it may ever claim (see detection/federated/admission.py).
    """
    try:
        record = get_server().admit_participant(
            req.participant_id, req.max_n_samples, req.admitted_by
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "admitted",
        "participant_id": record.participant_id,
        "max_n_samples": record.max_n_samples,
    }


@federated_app.post("/federated/update")
def http_submit_update(req: UpdateRequest) -> dict:
    soft_labels_bytes = base64.b64decode(req.soft_labels_b64)
    soft_labels = np.frombuffer(soft_labels_bytes, dtype=np.float64)
    signature = base64.b64decode(req.signature_b64)
    try:
        return get_server().submit_update(
            participant_id=req.participant_id,
            soft_labels=soft_labels,
            n_samples=req.n_samples,
            signature=signature,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@federated_app.get("/federated/global-model")
def http_global_model() -> dict:
    labels = get_server().get_global_soft_labels()
    if labels is None:
        return {"global_soft_labels_b64": None, "round_id": get_server().get_round_id()}
    return {
        "global_soft_labels_b64": base64.b64encode(labels.tobytes()).decode(),
        "round_id": get_server().get_round_id(),
    }


@federated_app.get("/federated/server-public-key")
def http_server_pubkey() -> dict:
    der = get_server().get_server_public_key_der()
    return {"public_key_der_b64": base64.b64encode(der).decode()}


# ---------------------------------------------------------------------------
# SMPC share aggregation endpoints  (Issue-138)
# ---------------------------------------------------------------------------


class SMPCShareRequest(BaseModel):
    aggregator_id: int
    participant_id: str
    share_b64: str
    commitment: str | None = None


_smpc_aggregators: dict[int, "SMPCAggregatorState"] = {}


class SMPCAggregatorState:
    def __init__(self, aggregator_id: int, n_aggregators: int = 3) -> None:
        from detection.federated.smpc import SMPCAggregator
        self._inner = SMPCAggregator(aggregator_id, n_aggregators)

    def receive(self, share_bytes: bytes, commitment: str | None) -> None:
        import numpy as np
        share = np.frombuffer(share_bytes, dtype=np.float64)
        self._inner.receive_share(share, commitment)

    def finalize(self) -> bytes:
        return self._inner.finalize().tobytes()

    def reset(self) -> None:
        self._inner.reset()


def _get_smpc_aggregator(aggregator_id: int) -> SMPCAggregatorState:
    if aggregator_id not in _smpc_aggregators:
        _smpc_aggregators[aggregator_id] = SMPCAggregatorState(aggregator_id)
    return _smpc_aggregators[aggregator_id]


@federated_app.post("/federated/smpc/share")
def smpc_receive_share(req: SMPCShareRequest) -> dict:
    """Accept a gradient share from a client for SMPC aggregation."""
    share_bytes = base64.b64decode(req.share_b64)
    agg = _get_smpc_aggregator(req.aggregator_id)
    try:
        agg.receive(share_bytes, req.commitment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "accepted", "aggregator_id": req.aggregator_id}


@federated_app.get("/federated/smpc/partial-sum/{aggregator_id}")
def smpc_get_partial_sum(aggregator_id: int) -> dict:
    """Return the partial sum for this aggregator to be exchanged with peers."""
    if aggregator_id not in _smpc_aggregators:
        raise HTTPException(status_code=404, detail=f"Aggregator {aggregator_id} has no shares")
    agg = _smpc_aggregators[aggregator_id]
    partial_sum_bytes = agg.finalize()
    return {"partial_sum_b64": base64.b64encode(partial_sum_bytes).decode()}


@federated_app.post("/federated/smpc/reconstruct")
def smpc_reconstruct(partial_sums_b64: list[str]) -> dict:
    """Reconstruct gradient from partial sums (any 2 of 3 suffice)."""
    import numpy as np
    from detection.federated.smpc import reconstruct_gradient
    if len(partial_sums_b64) < 2:
        raise HTTPException(status_code=400, detail="At least 2 partial sums required")
    partial_sums = [np.frombuffer(base64.b64decode(s), dtype=np.float64) for s in partial_sums_b64]
    gradient = reconstruct_gradient(partial_sums)
    return {"gradient_b64": base64.b64encode(gradient.tobytes()).decode()}
