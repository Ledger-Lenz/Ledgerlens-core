"""Off-chain ZK proof verifier (mirrors the Soroban verifier contract logic).

This module provides a pure-Python verification entry point that matches
the on-chain verification logic so that proofs can be tested locally.
"""

from __future__ import annotations


from detection.zk_prover import verify_threshold_proof, ProofError

__all__ = ["verify_threshold_proof", "ProofError"]
