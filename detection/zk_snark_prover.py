from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from config.settings import settings
from detection.zk_prover import ProofError

@dataclass
class SnarkProof:
    proof_a: tuple[int, int]
    proof_b: tuple[tuple[int, int], tuple[int, int]]
    proof_c: tuple[int, int]
    public_signals: list[int]   # [commitX, commitY, threshold]


def generate_snark_range_proof(
    score: int,
    blinding: int,
    commit: tuple[int, int],
    threshold: int,
) -> SnarkProof:
    """Writes a witness input JSON, invokes `snarkjs groth16 fullprove` (or wtns calculate + prove)
    against `circuits/keys/score_range_proof.zkey`, parses proof.json.
    Raises ProofError on subprocess failure or malformed output.
    """
    if not (0 <= score <= 100):
        raise ProofError(f"Score must be in [0, 100], got {score}")

    # Generate witness input dict
    input_data = {
        "score": str(score),
        "blinding": str(blinding),
        "commitX": str(commit[0]),
        "commitY": str(commit[1]),
        "threshold": str(threshold),
    }

    # Prepare temporary file paths securely
    fd, input_path = tempfile.mkstemp(suffix=".json")
    # Set permissions to 0o600 (owner read/write only)
    os.chmod(input_path, 0o600)

    proof_fd, proof_path = tempfile.mkstemp(suffix=".json")
    public_fd, public_path = tempfile.mkstemp(suffix=".json")

    # Close file descriptors so we can write/read via path
    os.close(fd)
    os.close(proof_fd)
    os.close(public_fd)

    try:
        # Write inputs to input_path
        with open(input_path, "w") as f:
            json.dump(input_data, f)

        zkey_path = settings.zk_snark_proving_key_path
        # We need the wasm file matching the circuit
        wasm_path = settings.zk_snark_circuit_path.replace(".circom", "_js/score_range_proof.wasm")
        if not os.path.exists(wasm_path):
            # Fallback path if generated differently
            wasm_path = "circuits/score_range_proof_js/score_range_proof.wasm"

        # Check proving key exists
        if not os.path.exists(zkey_path):
            raise ProofError(f"Proving key not found at: {zkey_path}")

        # Run snarkjs groth16 fullprove
        cmd = [
            "snarkjs",
            "groth16",
            "fullprove",
            input_path,
            wasm_path,
            zkey_path,
            proof_path,
            public_path,
        ]

        # In windows, snarkjs is usually run via cmd/shell if it's a node script
        # but subprocess with shell=True or finding the cmd executable is better.
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=settings.zk_snark_prover_timeout_seconds,
                shell=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProofError("Prover subprocess execution timed out.") from exc

        if result.returncode != 0:
            # Mask sensitive inputs from error messages
            stderr_cleaned = result.stderr.replace(str(score), "[REDACTED]").replace(str(blinding), "[REDACTED]")
            raise ProofError(f"snarkjs execution failed: {stderr_cleaned}")

        # Parse proof.json and public.json
        with open(proof_path, "r") as f:
            proof_json = json.load(f)
        with open(public_path, "r") as f:
            public_json = json.load(f)

        # Structure proof fields as expected SnarkProof
        pi_a = (int(proof_json["pi_a"][0]), int(proof_json["pi_a"][1]))
        pi_b = (
            (int(proof_json["pi_b"][0][1]), int(proof_json["pi_b"][0][0])),
            (int(proof_json["pi_b"][1][1]), int(proof_json["pi_b"][1][0])),
        )
        pi_c = (int(proof_json["pi_c"][0]), int(proof_json["pi_c"][1]))
        pub_signals = [int(x) for x in public_json]

        return SnarkProof(
            proof_a=pi_a,
            proof_b=pi_b,
            proof_c=pi_c,
            public_signals=pub_signals,
        )

    except Exception as e:
        if isinstance(e, ProofError):
            raise
        raise ProofError(f"Failed to generate snark proof: {str(e)}") from e

    finally:
        # Secure cleanup
        for path in (input_path, proof_path, public_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
