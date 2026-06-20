"""Soroban on-chain score publisher.

Submits RiskScore records to the ledgerlens-score Soroban contract,
making wash-trading scores natively queryable by other Soroban contracts
(AMMs, lending protocols, DEX aggregators).
"""

from __future__ import annotations

import logging
import threading
import time

from stellar_sdk import Keypair, SorobanServer, TransactionBuilder
from stellar_sdk import scval
from stellar_sdk.operation import InvokeHostFunction as InvokeContractFunction

from detection.risk_score import RiskScore
from detection.storage import save_submission

logger = logging.getLogger("ledgerlens.soroban")


class SorobanSubmissionError(Exception):
    """Unrecoverable Soroban submission failure."""


class SorobanCircuitOpenError(Exception):
    """Circuit breaker is open; submissions are temporarily blocked."""


class SorobanPublisher:
    """Publishes RiskScore records on-chain via the ledgerlens-score contract.

    Handles transaction construction, fee estimation via simulate_transaction,
    sequence-number management with tx_bad_seq retry, INSUFFICIENT_FEE retry,
    and a circuit breaker to prevent submission storms on contract failures.
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
    ):
        self._contract_id = contract_id
        self._soroban_rpc_url = soroban_rpc_url
        self._network_passphrase = network_passphrase
        self._keypair = Keypair.from_secret(secret_key)
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_window = circuit_breaker_window
        self._circuit_reset_seconds = circuit_reset_seconds
        self._failure_timestamps: list[float] = []
        self._lock = threading.Lock()

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("_keypair", None)
        return state

    def __repr__(self) -> str:
        cid = self._contract_id[:8] if len(self._contract_id) > 8 else self._contract_id
        return f"<SorobanPublisher contract_id={cid}...>"

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _check_circuit(self) -> None:
        """Raise SorobanCircuitOpenError if the circuit is open."""
        now = time.time()
        with self._lock:
            cutoff = now - self._circuit_breaker_window
            self._failure_timestamps = [t for t in self._failure_timestamps if t > cutoff]

            if len(self._failure_timestamps) >= self._circuit_breaker_threshold:
                if self._failure_timestamps and (now - self._failure_timestamps[0]) >= self._circuit_reset_seconds:
                    self._failure_timestamps.clear()
                    logger.info("Soroban circuit breaker reset after cooldown period")
                    return
                raise SorobanCircuitOpenError(
                    f"Circuit breaker open: {len(self._failure_timestamps)} failures "
                    f"within {self._circuit_breaker_window}s window"
                )

    def _record_failure(self) -> None:
        with self._lock:
            self._failure_timestamps.append(time.time())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_score(self, score: RiskScore, dry_run: bool = False) -> str | None:
        """Submit a single RiskScore to the on-chain registry.

        Returns the transaction hash on success, ``None`` on skip
        (``dry_run=True``).

        Raises SorobanSubmissionError on unrecoverable failure.
        Raises SorobanCircuitOpenError when the circuit breaker is open.
        """
        try:
            self._check_circuit()
        except SorobanCircuitOpenError:
            save_submission(score.wallet, score.asset_pair, score.score, "skipped", error_message="Circuit breaker open")
            raise

        if dry_run:
            save_submission(score.wallet, score.asset_pair, score.score, "skipped")
            return None

        server = SorobanServer(self._soroban_rpc_url)
        try:
            tx_hash = self._execute_with_retries(server, score)
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

    def _execute_with_retries(self, server: SorobanServer, score: RiskScore) -> str:
        """Attempt submission with retry logic for transient errors.

        Retries once for:
          * ``tx_bad_seq`` — refreshes the account sequence via a fresh load
          * ``INSUFFICIENT_FEE`` — multiplies the fee by 1.5
          * polling timeout — full retry

        Does **not** retry Soroban auth failures; raises immediately.
        """
        for attempt in range(1, 3):
            tx_hash, error = self._submit_once(server, score, fee_multiplier=1.0)

            if error is None:
                return tx_hash

            error_lower = error.lower()

            # Auth failures are unrecoverable — never retry
            if "auth" in error_lower and "failed" in error_lower:
                self._record_failure()
                logger.error("Soroban auth failed - check service_secret_key configuration")
                raise SorobanSubmissionError(error)

            if attempt == 1:
                if "bad_seq" in error_lower:
                    logger.warning("tx_bad_seq, refreshing sequence and retrying")
                    continue
                if "insufficient_fee" in error_lower:
                    logger.warning("INSUFFICIENT_FEE, retrying with 1.5x fee")
                    tx_hash, error = self._submit_once(server, score, fee_multiplier=1.5)
                    if error is None:
                        return tx_hash
                    break
                if "timeout" in error_lower:
                    logger.warning("Transaction polling timed out, retrying")
                    continue

            break

        self._record_failure()
        raise SorobanSubmissionError(error or "Submission failed after retries")

    def _submit_once(
        self,
        server: SorobanServer,
        score: RiskScore,
        fee_multiplier: float = 1.0,
    ) -> tuple[str | None, str | None]:
        """Submit a single score without retries.

        Returns ``(tx_hash, None)`` on success or ``(None, error_msg)`` on
        failure.  Callers implement retry logic.
        """
        try:
            source_account = server.load_account(self._keypair.public_key)

            op = InvokeContractFunction(
                contract_id=self._contract_id,
                function_name="submit_score",
                parameters=[
                    scval.to_address(score.wallet),
                    scval.to_symbol(score.asset_pair),
                    scval.to_uint32(max(0, min(100, score.score))),
                    scval.to_uint64(int(score.timestamp.timestamp())),
                ],
            )

            tx = (
                TransactionBuilder(
                    source_account=source_account,
                    network_passphrase=self._network_passphrase,
                    base_fee=100,
                )
                .add_operation(op)
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
