"""Soroban on-chain score publisher.

Submits RiskScore records to the ledgerlens-score Soroban contract,
making wash-trading scores natively queryable by other Soroban contracts
(AMMs, lending protocols, DEX aggregators).

Zero-knowledge support
----------------------
Each submission now additionally includes:
  - A SHA-256 commitment binding the wallet, score, and raw feature vector.
  - A Pedersen commitment (BN254 curve point) for threshold proofs.
  - A serialised ZK sigma-protocol proof (generated ahead of time for
    a configurable default threshold).

Downstream contracts can call ``verify_threshold(wallet, T, π)`` on
the Soroban verifier contract to check ``score >= T`` without learning
the raw score or any feature value.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from stellar_sdk import Keypair, SorobanServer, TransactionBuilder
from stellar_sdk import scval

from detection.risk_score import RiskScore
from detection.storage import save_submission, init_db, _connect
from detection.zk_commitment import (
    generate_salt,
)
from detection.zk_prover import generate_threshold_proof
from config.settings import settings

logger = logging.getLogger("ledgerlens.soroban")

_DLQ_MAX_ROWS_DEFAULT = 10000

# ---------------------------------------------------------------------------
# DLQ SQLite schema (created via db-migrate migration 14)
# ---------------------------------------------------------------------------
_DLQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS soroban_dead_letters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    asset_pair      TEXT NOT NULL,
    score           INTEGER NOT NULL,
    ledger_timestamp INTEGER NOT NULL,
    error_message   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'replayed', 'failed')),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    replayed_at     TIMESTAMP,
    replay_tx_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_dlq_status ON soroban_dead_letters(status);
CREATE INDEX IF NOT EXISTS idx_dlq_created ON soroban_dead_letters(created_at);
"""


def init_dlq_schema(db_path: str | None = None) -> None:
    """Ensure the soroban_dead_letters table exists."""
    path = db_path or settings.db_path
    with sqlite3.connect(path) as conn:
        conn.executescript(_DLQ_SCHEMA)
        conn.commit()


def get_dlq_pending_count(db_path: str | None = None) -> int:
    """Return the number of pending DLQ rows."""
    try:
        init_dlq_schema(db_path)
        path = db_path or settings.db_path
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM soroban_dead_letters WHERE status = 'pending'"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def get_dlq_entries(
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db_path: str | None = None,
) -> tuple[list[dict], int]:
    """Return paginated DLQ entries and total count."""
    init_dlq_schema(db_path)
    path = db_path or settings.db_path
    where = ""
    params: list = []
    if status is not None:
        where = "WHERE status = ?"
        params.append(status)
    offset = (page - 1) * page_size

    with sqlite3.connect(path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM soroban_dead_letters {where}", tuple(params)
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, wallet, asset_pair, score, ledger_timestamp, error_message, "
            f"status, created_at, replayed_at, replay_tx_hash "
            f"FROM soroban_dead_letters {where} ORDER BY created_at ASC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, offset),
        ).fetchall()

    entries = [
        {
            "id": r[0],
            "wallet": r[1],
            "asset_pair": r[2],
            "score": r[3],
            "ledger_timestamp": r[4],
            "error_message": r[5],
            "status": r[6],
            "created_at": r[7],
            "replayed_at": r[8],
            "replay_tx_hash": r[9],
        }
        for r in rows
    ]
    return entries, total


@dataclass
class SorobanHealthStatus:
    """Snapshot of the Soroban circuit breaker state."""
    circuit_state: str            # "closed" | "open" | "half-open"
    consecutive_failures: int
    last_error: Optional[str]     # None if no failure yet
    circuit_opened_at: Optional[datetime]
    seconds_until_reset: Optional[float]   # None when closed
    dlq_pending_count: int


class SorobanSubmissionError(Exception):
    """Unrecoverable Soroban submission failure."""


class SorobanCircuitOpenError(Exception):
    """Circuit breaker is open; submissions are temporarily blocked."""


class SorobanPublisher:
    """Publishes RiskScore records on-chain via the ledgerlens-score contract.

    Handles transaction construction, fee estimation via simulate_transaction,
    sequence-number management with tx_bad_seq retry, INSUFFICIENT_FEE retry,
    and a circuit breaker to prevent submission storms on contract failures.

    Circuit breaker states:
      closed    → normal operation
      open      → blocking submissions; DLQ writes instead
      half-open → probe mode after reset timeout; one probe attempt

    When *features* are provided, a SHA-256 commitment and a BN254 Pedersen
    commitment are generated and published alongside the score, enabling
    zero-knowledge threshold verification on-chain.
    """

    def __init__(
        self,
        contract_id: str,
        secret_key: str,
        soroban_rpc_url: str,
        network_passphrase: str,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_window: int = 60,
        circuit_reset_seconds: int = 300,
        default_threshold: int = 70,
        db_path: str | None = None,
    ):
        self._contract_id = contract_id
        self._soroban_rpc_url = soroban_rpc_url
        self._network_passphrase = network_passphrase
        self._keypair = Keypair.from_secret(secret_key)
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_window = circuit_breaker_window
        self._circuit_reset_seconds = circuit_reset_seconds
        self._lock = threading.Lock()
        self._default_threshold = default_threshold
        self._db_path = db_path

        # --- Extended circuit breaker state ---
        self._circuit_state: str = "closed"       # closed | open | half-open
        self._consecutive_failures: int = 0
        self._last_error: Optional[str] = None
        self._circuit_opened_at: Optional[float] = None  # monotonic timestamp

        # Legacy: kept for backward compat but superseded by new state machine
        self._failure_timestamps: list[float] = []

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("_keypair", None)
        return state

    def __repr__(self) -> str:
        cid = self._contract_id[:8] if len(self._contract_id) > 8 else self._contract_id
        return f"<SorobanPublisher contract_id={cid}...>"

    # ------------------------------------------------------------------
    # Health / manual reset
    # ------------------------------------------------------------------

    def health(self) -> SorobanHealthStatus:
        """Thread-safe snapshot of circuit breaker state."""
        with self._lock:
            state = self._circuit_state
            opened_at = self._circuit_opened_at
            secs_until_reset: Optional[float] = None
            opened_at_dt: Optional[datetime] = None

            if state in ("open", "half-open") and opened_at is not None:
                elapsed = time.monotonic() - opened_at
                remaining = self._circuit_reset_seconds - elapsed
                secs_until_reset = max(0.0, remaining)
                opened_at_dt = datetime.fromtimestamp(
                    time.time() - (time.monotonic() - opened_at), tz=timezone.utc
                )

        dlq_count = get_dlq_pending_count(self._db_path)
        return SorobanHealthStatus(
            circuit_state=state,
            consecutive_failures=self._consecutive_failures,
            last_error=self._last_error,
            circuit_opened_at=opened_at_dt,
            seconds_until_reset=secs_until_reset,
            dlq_pending_count=dlq_count,
        )

    def reset_circuit(self) -> SorobanHealthStatus:
        """Immediately close the circuit, clear failure counters and last_error.

        Logs at WARNING level with the event type so resets are auditable.
        Returns new health snapshot.
        """
        with self._lock:
            self._circuit_state = "closed"
            self._consecutive_failures = 0
            self._last_error = None
            self._circuit_opened_at = None
            self._failure_timestamps.clear()
        logger.warning("Circuit manually reset by operator.")
        return self.health()

    # ------------------------------------------------------------------
    # Circuit breaker state machine
    # ------------------------------------------------------------------

    def _check_circuit(self) -> None:
        """Raise SorobanCircuitOpenError if the circuit is open.

        Transitions open → half-open when the reset timeout has elapsed.
        """
        now_mono = time.monotonic()
        with self._lock:
            state = self._circuit_state

            if state == "closed":
                return

            if state == "open" and self._circuit_opened_at is not None:
                elapsed = now_mono - self._circuit_opened_at
                if elapsed >= self._circuit_reset_seconds:
                    self._circuit_state = "half-open"
                    logger.info(
                        "Soroban circuit breaker transitioning open → half-open (probe mode)."
                    )
                    return  # allow the probe through

            if self._circuit_state == "half-open":
                return  # allow the single probe through

            # Still open
            raise SorobanCircuitOpenError(
                f"Circuit breaker open: {self._consecutive_failures} consecutive failures"
            )

    def _record_failure(self, error: str = "") -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = error or self._last_error
            self._failure_timestamps.append(time.time())

            if self._circuit_state == "half-open":
                # Probe failed → back to open, reset timer
                self._circuit_state = "open"
                self._circuit_opened_at = time.monotonic()
                logger.warning(
                    "Half-open probe failed; circuit re-opened. Error: %s", error
                )
            elif self._consecutive_failures >= self._circuit_breaker_threshold:
                self._circuit_state = "open"
                self._circuit_opened_at = time.monotonic()
                logger.error(
                    "Soroban circuit breaker opened after %d consecutive failures.",
                    self._consecutive_failures,
                )

    def _record_success(self) -> None:
        with self._lock:
            if self._circuit_state == "half-open":
                self._circuit_state = "closed"
                logger.info("Half-open probe succeeded; circuit closed.")
            self._consecutive_failures = 0
            self._last_error = None
            self._circuit_opened_at = None

    # ------------------------------------------------------------------
    # Dead-letter queue
    # ------------------------------------------------------------------

    def _write_dead_letter(
        self,
        wallet: str,
        asset_pair: str,
        score: int,
        timestamp: int,
        error: str,
    ) -> None:
        """Write a failed submission to soroban_dead_letters with status='pending'."""
        try:
            init_dlq_schema(self._db_path)
            db_path = self._db_path or settings.db_path
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO soroban_dead_letters "
                    "(wallet, asset_pair, score, ledger_timestamp, error_message, status) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (wallet, asset_pair, score, timestamp, error),
                )
                conn.commit()

            # Prune if over cap
            max_rows = int(getattr(settings, "soroban_dlq_max_rows", _DLQ_MAX_ROWS_DEFAULT))
            with sqlite3.connect(db_path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM soroban_dead_letters"
                ).fetchone()[0]
                if count > max_rows:
                    conn.execute(
                        f"DELETE FROM soroban_dead_letters WHERE id IN ("
                        f"SELECT id FROM soroban_dead_letters ORDER BY created_at ASC LIMIT {count - max_rows})"
                    )
                    conn.commit()
        except Exception as exc:
            logger.error("Failed to write DLQ entry: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_score(
        self,
        score: RiskScore,
        dry_run: bool = False,
        features: dict | None = None,
    ) -> str | None:
        """Submit a single RiskScore to the on-chain registry.

        When *features* is provided, a SHA-256 commitment and BN254
        Pedersen commitment are generated and attached to the submission
        so that downstream contracts can verify ``score >= threshold``
        without seeing the raw score or features.

        Returns the transaction hash on success, ``None`` on skip
        (``dry_run=True``).

        Raises SorobanSubmissionError on unrecoverable failure.
        Raises SorobanCircuitOpenError when the circuit breaker is open.
        """
        try:
            self._check_circuit()
        except SorobanCircuitOpenError as exc:
            save_submission(score.wallet, score.asset_pair, score.score, "skipped", error_message="Circuit breaker open")
            self._write_dead_letter(
                wallet=score.wallet,
                asset_pair=score.asset_pair,
                score=score.score,
                timestamp=int(score.timestamp.timestamp()),
                error=str(exc),
            )
            raise

        # Build cryptographic commitment / ZK proof bundle ---------------------------------
        zk_bundle = self._build_zk_bundle(score, features) if features else None

        if dry_run:
            save_submission(score.wallet, score.asset_pair, score.score, "skipped")
            return None

        server = SorobanServer(self._soroban_rpc_url)
        try:
            tx_hash = self._execute_with_retries(server, score, zk_bundle=zk_bundle)
            self._record_success()
            save_submission(score.wallet, score.asset_pair, score.score, "submitted", tx_hash=tx_hash)
            return tx_hash
        except SorobanSubmissionError:
            save_submission(score.wallet, score.asset_pair, score.score, "failed", error_message="Submission failed after retries")
            raise
        except Exception:
            save_submission(score.wallet, score.asset_pair, score.score, "failed", error_message="Unexpected submission error")
            raise
        finally:
            server.close()

    def submit_with_quorum(
        self,
        wallet: str,
        asset_pair: str,
        score: int,
        timestamp: int,
        quorum: QuorumSignature,
    ) -> bool:
        """Submit a multi-signature quorum for on-chain verification."""
        try:
            self._check_circuit()
        except SorobanCircuitOpenError:
            save_submission(wallet, asset_pair, score, "skipped", error_message="Circuit breaker open")
            return False

        server = SorobanServer(self._soroban_rpc_url)
        try:
            source_account = server.load_account(self._keypair.public_key)

            # Convert signatures to Soroban Vector of pairs
            sig_pairs = []
            for pub, sig in quorum.signatures:
                sig_pairs.append(
                    scval.to_vec([
                        scval.to_bytes(bytes.fromhex(pub)),
                        scval.to_bytes(bytes.fromhex(sig)),
                    ])
                )

            params = [
                scval.to_address(wallet),
                scval.to_symbol(asset_pair),
                scval.to_uint32(max(0, min(100, score))),
                scval.to_uint64(timestamp),
                scval.to_vec(sig_pairs),
            ]

            tx = (
                TransactionBuilder(
                    source_account=source_account,
                    network_passphrase=self._network_passphrase,
                    base_fee=100,
                )
                .append_invoke_contract_function_op(
                    contract_id=self._contract_id,
                    function_name="submit_with_quorum",
                    parameters=params,
                )
                .build()
            )

            sim_result = server.simulate_transaction(tx)
            if sim_result.error:
                logger.error("Simulation failed for submit_with_quorum: %s", sim_result.error)
                return False

            fee = int(int(sim_result.min_resource_fee) * 1.0)
            tx.set_transaction_fee(fee)
            tx.sign(self._keypair)

            send_result = server.send_transaction(tx)
            if send_result.status == "ERROR":
                logger.error("Send failed for submit_with_quorum: %s", send_result.error)
                return False

            tx_hash = send_result.hash

            for _ in range(30):
                get_result = server.get_transaction(tx_hash)
                if get_result.status == "SUCCESS":
                    save_submission(wallet, asset_pair, score, "submitted", tx_hash=tx_hash)
                    return True
                if get_result.status == "FAILED":
                    logger.error("Transaction failed: %s", get_result)
                    return False
                time.sleep(1)

            logger.error("Transaction polling timeout")
            return False
        except Exception as e:
            logger.error("Unexpected error in submit_with_quorum: %s", e)
            return False
        finally:
            server.close()

    def submit_batch(self, scores: list[RiskScore], dry_run: bool = False) -> dict[str, str]:
        """Submit a list of scores.

        Returns a dict mapping ``{wallet}:{asset_pair}`` to either a
        transaction hash (success) or an ``"ERROR: ..."`` string (failure).
        """
        results: dict[str, str] = {}
        for s in scores:
            key = f"{s.wallet}:{s.asset_pair}"
            try:
                tx_hash = self.submit_score(s, dry_run=dry_run)
                results[key] = tx_hash or "skipped"
            except SorobanCircuitOpenError as e:
                logger.warning("Circuit breaker opened mid-batch; stopping submissions")
                results[key] = f"ERROR: {e}"
                break
            except SorobanSubmissionError as e:
                results[key] = f"ERROR: {e}"
            except Exception as e:
                logger.warning("Unexpected error submitting score: %s", e)
                results[key] = f"ERROR: {e}"
        return results

    # ------------------------------------------------------------------
    # Internal submission helpers
    # ------------------------------------------------------------------

    def _build_zk_bundle(self, score: RiskScore, features: dict) -> dict:
        """Generate a SHA-256 commitment + Pedersen commitment + threshold proof.

        Returns a dict with keys ``commitment_hex``, ``pedersen_x``,
        ``pedersen_y``, ``proof``, and ``salt``.
        """
        salt = generate_salt()
        threshold = self._default_threshold

        comm_hex, (px, py), proof = generate_threshold_proof(
            wallet=score.wallet,
            score=score.score,
            features=features,
            salt=salt,
            threshold=threshold,
        )
        return {
            "commitment_hex": comm_hex,
            "pedersen_x": px,
            "pedersen_y": py,
            "proof": proof,
            "salt": salt,
        }

    def _execute_with_retries(
        self,
        server: SorobanServer,
        score: RiskScore,
        zk_bundle: dict | None = None,
    ) -> str:
        """Attempt submission with retry logic for transient errors.

        Retries once for:
          * ``tx_bad_seq`` — refreshes the account sequence via a fresh load
          * ``INSUFFICIENT_FEE`` — multiplies the fee by 1.5
          * polling timeout — full retry

        Does **not** retry Soroban auth failures; raises immediately.
        """
        for attempt in range(1, 3):
            tx_hash, error = self._submit_once(server, score, fee_multiplier=1.0, zk_bundle=zk_bundle)

            if error is None:
                return tx_hash

            error_lower = error.lower()

            # Auth failures are unrecoverable — never retry
            if "auth" in error_lower and "failed" in error_lower:
                self._record_failure(error)
                logger.error("Soroban auth failed - check service_secret_key configuration")
                raise SorobanSubmissionError(error)

            if attempt == 1:
                if "bad_seq" in error_lower:
                    logger.warning("tx_bad_seq, refreshing sequence and retrying")
                    continue
                if "insufficient_fee" in error_lower:
                    logger.warning("INSUFFICIENT_FEE, retrying with 1.5x fee")
                    tx_hash, error = self._submit_once(server, score, fee_multiplier=1.5, zk_bundle=zk_bundle)
                    if error is None:
                        return tx_hash
                    break
                if "timeout" in error_lower:
                    logger.warning("Transaction polling timed out, retrying")
                    continue

            break

        self._record_failure(error or "Submission failed after retries")
        raise SorobanSubmissionError(error or "Submission failed after retries")

    def _submit_once(
        self,
        server: SorobanServer,
        score: RiskScore,
        fee_multiplier: float = 1.0,
        zk_bundle: dict | None = None,
    ) -> tuple[str | None, str | None]:
        """Submit a single score without retries.

        When *zk_bundle* is provided, the contract invocation is extended
        with ``commitment_hash``, ``pedersen_x``, and ``pedersen_y`` so
        that the on-chain verifier can later check threshold proofs.

        Returns ``(tx_hash, None)`` on success or ``(None, error_msg)`` on
        failure.  Callers implement retry logic.
        """
        try:
            source_account = server.load_account(self._keypair.public_key)

            params = [
                scval.to_address(score.wallet),
                scval.to_symbol(score.asset_pair),
                scval.to_uint32(max(0, min(100, score.score))),
                scval.to_uint64(int(score.timestamp.timestamp())),
            ]

            if zk_bundle:
                params.extend([
                    scval.to_string(zk_bundle["commitment_hex"]),
                    scval.to_uint256(zk_bundle["pedersen_x"]),
                    scval.to_uint256(zk_bundle["pedersen_y"]),
                ])

            tx = (
                TransactionBuilder(
                    source_account=source_account,
                    network_passphrase=self._network_passphrase,
                    base_fee=100,
                )
                .append_invoke_contract_function_op(
                    contract_id=self._contract_id,
                    function_name="submit_score",
                    parameters=params,
                )
                .build()
            )

            sim_result = server.simulate_transaction(tx)

            if sim_result.error:
                error_str = str(sim_result.error)
                return None, error_str

            fee = int(int(sim_result.min_resource_fee) * fee_multiplier)
            tx.set_transaction_fee(fee)
            tx.sign(self._keypair)

            send_result = server.send_transaction(tx)

            if send_result.status == "ERROR":
                error_str = str(send_result.error or "send failed")
                return None, error_str

            tx_hash = send_result.hash

            for _ in range(30):
                get_result = server.get_transaction(tx_hash)
                if get_result.status == "SUCCESS":
                    return tx_hash, None
                if get_result.status == "FAILED":
                    err = str(
                        getattr(get_result, "result_xdr", "")
                        or getattr(get_result, "error", "")
                        or "execution failed"
                    )
                    return None, err
                time.sleep(1)

            return None, "timeout: polling exceeded 30 attempts"
        except SorobanSubmissionError:
            raise
        except Exception as e:
            return None, str(e)
