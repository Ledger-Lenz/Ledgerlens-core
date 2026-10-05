"""Off-chain ZK proof verifier (mirrors the Soroban verifier contract logic).

This module provides a pure-Python verification entry point that matches
the on-chain verification logic so that proofs can be tested locally.

Threat model
------------
The verifier must reject two classes of adversarial inputs that a naive
verifier would accept:

* **Replay** - a proof that was valid for one context (score/threshold,
  nonce, verifier address) must not verify against a different context.
  Context binding is enforced by folding the context into the challenge
  derivation, so any change to the context invalidates the proof.
* **Malleability** - a proof must not be transformable into a *different*
  but still-valid proof. We reject non-canonical encodings (leading zero
  bytes, out-of-range scalars) and require the challenge to be derived
  deterministically from the proof transcript.

See ``docs/threat_model.md`` for the full write-up.
"""

from __future__ import annotations


from detection.zk_prover import verify_threshold_proof, ProofError

__all__ = ["verify_threshold_proof", "ProofError"]
