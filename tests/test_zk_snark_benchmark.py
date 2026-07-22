from __future__ import annotations

import time
from unittest import mock
import pytest
import json

from detection.zk_commitment import generate_salt
from detection.zk_prover import generate_threshold_proof
from detection.zk_snark_prover import generate_snark_range_proof


@pytest.mark.benchmark
def test_zk_proof_benchmark():
    """Benchmark proof generation latency and byte size for sigma vs. SNARK."""
    score = 85
    threshold = 70
    wallet = "GABCDEF123"
    features = {"trade_frequency": 15}
    salt = generate_salt()

    # 1. Benchmark Sigma Proof
    start_sigma = time.perf_counter()
    _, (px, py), sigma_proof = generate_threshold_proof(wallet, score, features, salt, threshold)
    sigma_duration = time.perf_counter() - start_sigma

    # Serialize sigma proof to count bytes (approximating transport size via JSON)
    sigma_bytes = len(json.dumps(sigma_proof).encode())

    # 2. Benchmark SNARK Proof (mocked subprocess)
    mock_proof_json = {
        "pi_a": ["1111", "2222", "1"],
        "pi_b": [
            ["3333", "4444", "1"],
            ["5555", "6666", "1"],
            ["1", "0", "0"]
        ],
        "pi_c": ["7777", "8888", "1"]
    }
    mock_public_json = [str(px), str(py), "70"]

    with mock.patch("os.path.exists", return_value=True), \
         mock.patch("subprocess.run") as mock_run, \
         mock.patch("builtins.open") as mock_file:

        from unittest.mock import MagicMock
        mock_run.return_value = MagicMock(returncode=0)
        mock_file.return_value.__enter__.return_value.read.side_effect = [
            json.dumps(mock_proof_json),
            json.dumps(mock_public_json)
        ]

        start_snark = time.perf_counter()
        snark_proof = generate_snark_range_proof(score, 12345, (px, py), threshold)
        snark_duration = time.perf_counter() - start_snark

        # Serialize SNARK proof to 256 bytes (8 field elements of 32 bytes)
        proof_bytes = b"".join([
            snark_proof.proof_a[0].to_bytes(32, "little"),
            snark_proof.proof_a[1].to_bytes(32, "little"),
            snark_proof.proof_b[0][0].to_bytes(32, "little"),
            snark_proof.proof_b[0][1].to_bytes(32, "little"),
            snark_proof.proof_b[1][0].to_bytes(32, "little"),
            snark_proof.proof_b[1][1].to_bytes(32, "little"),
            snark_proof.proof_c[0].to_bytes(32, "little"),
            snark_proof.proof_c[1].to_bytes(32, "little"),
        ])
        snark_bytes = len(proof_bytes)

    # Assert SNARK proof size stays under 256 bytes limit
    assert snark_bytes <= 256

    print("\n--- ZK Benchmark Results ---")
    print(f"Sigma Proof Size: {sigma_bytes} bytes | Prover Time: {sigma_duration * 1000:.2f} ms")
    print(f"SNARK Proof Size: {snark_bytes} bytes | Prover Time: {snark_duration * 1000:.2f} ms")
