from __future__ import annotations

import json
import subprocess
from unittest import mock
import pytest

from detection.zk_prover import ProofError
from detection.zk_snark_prover import generate_snark_range_proof, SnarkProof


@pytest.fixture
def mock_proof_json():
    return {
        "pi_a": ["1111", "2222", "1"],
        "pi_b": [
            ["3333", "4444", "1"],
            ["5555", "6666", "1"],
            ["1", "0", "0"]
        ],
        "pi_c": ["7777", "8888", "1"],
        "protocol": "groth16"
    }


@pytest.fixture
def mock_public_json():
    return ["12345", "67890", "70"]


def test_generate_snark_range_proof_success(mock_proof_json, mock_public_json):
    """Test that proof generation successfully runs and parses outputs from snarkjs."""
    with mock.patch("os.path.exists", return_value=True), \
         mock.patch("subprocess.run") as mock_run, \
         mock.patch("builtins.open", mock.mock_open()) as mock_file:

        # Mock the json load calls
        mock_file.return_value.__enter__.return_value.read.side_effect = [
            json.dumps(mock_proof_json),
            json.dumps(mock_public_json)
        ]

        # Mock successful subprocess execution
        mock_run.return_value = mock.MagicMock(returncode=0)

        proof = generate_snark_range_proof(
            score=85,
            blinding=12345,
            commit=(12345, 67890),
            threshold=70
        )

        assert isinstance(proof, SnarkProof)
        assert proof.proof_a == (1111, 2222)
        assert proof.proof_b == ((4444, 3333), (6666, 5555))
        assert proof.proof_c == (7777, 8888)
        assert proof.public_signals == [12345, 67890, 70]


def test_generate_snark_range_proof_invalid_inputs():
    """Test that out of bounds score raises ProofError."""
    with pytest.raises(ProofError) as excinfo:
        generate_snark_range_proof(
            score=150,  # score must be in [0, 100]
            blinding=12345,
            commit=(12345, 67890),
            threshold=70
        )
    assert "Score must be in" in str(excinfo.value)


def test_generate_snark_range_proof_subprocess_failure():
    """Test that subprocess failure raises ProofError and redacts sensitive inputs."""
    with mock.patch("os.path.exists", return_value=True), \
         mock.patch("subprocess.run") as mock_run, \
         mock.patch("builtins.open", mock.mock_open()):

        # Mock failed subprocess execution
        mock_run.return_value = mock.MagicMock(
            returncode=1,
            stderr="Failed to compute witness for score 85 and blinding 999999"
        )

        with pytest.raises(ProofError) as excinfo:
            generate_snark_range_proof(
                score=85,
                blinding=999999,
                commit=(12345, 67890),
                threshold=70
            )

        err_msg = str(excinfo.value)
        assert "snarkjs execution failed" in err_msg
        assert "85" not in err_msg
        assert "999999" not in err_msg
        assert "[REDACTED]" in err_msg


def test_generate_snark_range_proof_timeout():
    """Test that prover subprocess timeout is handled cleanly."""
    with mock.patch("os.path.exists", return_value=True), \
         mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="snarkjs", timeout=10.0)), \
         mock.patch("builtins.open", mock.mock_open()):

        with pytest.raises(ProofError) as excinfo:
            generate_snark_range_proof(
                score=85,
                blinding=12345,
                commit=(12345, 67890),
                threshold=70
            )

        assert "timed out" in str(excinfo.value)
