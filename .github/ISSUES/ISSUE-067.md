---
title: "Implement Pedersen Commitment ZK Scheme for Score-Threshold Proofs"
labels: ["difficulty: advanced", "area: detection", "type: feature"]
assignees: []
---

## Summary

Extend `detection/zk_commitment.py` to implement a Pedersen commitment scheme enabling a wallet to prove cryptographically that its LedgerLens risk score is below a configurable threshold — without revealing the exact score. Implement `commit(score, randomness)`, `open(commitment, score, randomness)`, and `verify_below_threshold(commitment, threshold, proof)`. This enables privacy-preserving score attestation for DeFi protocol integrations: a protocol can verify a wallet meets its risk requirement without LedgerLens needing to reveal the raw score.

## Background & Context

LedgerLens currently exposes raw risk scores (0–100) via its REST API and on-chain Soroban contract. For some DeFi protocol integrations, this creates a privacy concern: the raw score reveals LedgerLens's internal model assessment of a wallet, which could be commercially sensitive or enable gamification (a wallet operator who knows their exact score can tune their behaviour to stay just below a threshold).

Pedersen commitments provide a solution. A Pedersen commitment to a value `v` using randomness `r` is `C = g^v * h^r mod p` (in a multiplicative group), where `g` and `h` are public group generators and `r` is a secret blinding factor known only to the committer. The commitment is:
- **Hiding**: `C` reveals nothing about `v` (computationally, assuming DLP hardness).
- **Binding**: it is computationally infeasible to open `C` to two different values.

A threshold proof (`score < threshold`) can be constructed as a range proof: prove that `v` lies in `[0, threshold-1]` without revealing `v`. For this implementation, use a simplified Sigma protocol (Schnorr-style interactive proof made non-interactive via Fiat-Shamir heuristic) over a 256-bit prime-order group.

The implementation should use the `py_ecc` library (already used for BN254 curves in the ZK prover module) or a dedicated Pedersen implementation over the Ristretto255 curve for cryptographic soundness. The threshold proof should be amenable to on-chain verification in the Soroban contract (future work — see ISSUE-080).

## Objectives

- [ ] Implement `PedersenParams` dataclass holding group parameters `(p, g, h, q)` for a 256-bit prime-order group (or Ristretto255 curve point generators).
- [ ] Implement `PedersenCommitment` dataclass holding `(C, r)` where `C` is the commitment value and `r` is the randomness (blinding factor).
- [ ] Implement `commit(score: int, randomness: Optional[int] = None) -> PedersenCommitment` generating cryptographically random `r` if not provided.
- [ ] Implement `open(commitment: PedersenCommitment, score: int, randomness: int) -> bool` verifying that `C == g^score * h^r mod p`.
- [ ] Implement `ThresholdProof` dataclass for the non-interactive Sigma protocol output.
- [ ] Implement `prove_below_threshold(score: int, threshold: int, commitment: PedersenCommitment) -> ThresholdProof` using Fiat-Shamir heuristic.
- [ ] Implement `verify_below_threshold(commitment: PedersenCommitment, threshold: int, proof: ThresholdProof) -> bool`.
- [ ] Expose `POST /scores/{wallet}/commit` endpoint that returns a fresh commitment and proof for the wallet's current score against a caller-specified threshold.
- [ ] Expose `POST /scores/verify-threshold` accepting commitment + proof + threshold and returning `{"valid": bool}`.
- [ ] All arithmetic must use constant-time operations where possible to prevent timing side-channels.
- [ ] Write unit tests verifying correctness, binding, and hiding properties.

## Technical Requirements

### Group parameters (`detection/zk_commitment.py`)

Use a well-known 256-bit prime-order group. For initial implementation, use the `cryptography` library's `ec` module with NIST P-256, or Ristretto255 via `ristretto255` package. Store group parameters in `config/settings.py` as a static constant (not user-configurable to prevent parameter substitution attacks).

```python
from dataclasses import dataclass
from typing import Optional
import secrets

@dataclass(frozen=True)
class PedersenParams:
    # For discrete-log based (multiplicative group) implementation:
    p: int          # large prime modulus
    q: int          # prime order of subgroup (p = 2q + 1 safe prime)
    g: int          # generator of order-q subgroup
    h: int          # independent generator: h = g^x for unknown x (discrete log of h is unknown)

@dataclass
class PedersenCommitment:
    C: int          # commitment value: g^score * h^r mod p
    r: int          # blinding factor (secret; do not transmit)

@dataclass
class ThresholdProof:
    # Non-interactive Sigma protocol (Fiat-Shamir)
    commitment: int         # the original Pedersen commitment C
    threshold: int
    challenge: int          # Fiat-Shamir challenge hash
    response: int           # prover's response to the challenge
    range_commitments: list[int]  # bit commitments for range proof
    range_responses: list[int]
```

### Core commitment functions

```python
class PedersenScheme:
    def __init__(self, params: PedersenParams):
        self.params = params

    def commit(self, score: int, randomness: Optional[int] = None) -> PedersenCommitment:
        """
        C = g^score * h^r mod p
        If randomness is None, generate cryptographically random r in [1, q-1].
        Raises ValueError if score not in [0, 100].
        """
        if not 0 <= score <= 100:
            raise ValueError(f"Score must be in [0, 100], got {score}")
        r = randomness if randomness is not None else secrets.randbelow(self.params.q - 1) + 1
        C = (pow(self.params.g, score, self.params.p) * pow(self.params.h, r, self.params.p)) % self.params.p
        return PedersenCommitment(C=C, r=r)

    def open(self, commitment: PedersenCommitment, score: int, randomness: int) -> bool:
        """Verify that C == g^score * h^r mod p."""
        expected = (pow(self.params.g, score, self.params.p) * pow(self.params.h, randomness, self.params.p)) % self.params.p
        # Constant-time comparison
        return secrets.compare_digest(
            expected.to_bytes(32, "big"), commitment.C.to_bytes(32, "big")
        )

    def prove_below_threshold(
        self, score: int, threshold: int, commitment: PedersenCommitment
    ) -> ThresholdProof:
        """
        Prove score < threshold using a bit-decomposition range proof.
        Decomposes (threshold - 1 - score) into bits and commits to each bit.
        Fiat-Shamir challenge: SHA-256(C || threshold || bit_commitments).
        """
        ...

    def verify_below_threshold(
        self, commitment: PedersenCommitment, threshold: int, proof: ThresholdProof
    ) -> bool:
        """Verify a threshold proof without learning the score."""
        ...
```

### API endpoints (`api/main.py`)

```python
class ThresholdCommitRequest(BaseModel):
    threshold: int = Field(..., ge=1, le=100)

class ThresholdCommitResponse(BaseModel):
    wallet: str
    threshold: int
    commitment_hex: str     # hex-encoded C (do NOT include r)
    proof: dict             # serialised ThresholdProof (no blinding factor)
    valid_until: datetime   # expires after 1 hour

@router.post("/scores/{wallet}/commit", response_model=ThresholdCommitResponse)
async def commit_score_threshold(
    wallet: str,
    body: ThresholdCommitRequest,
    db: RiskScoreStore = Depends(get_db),
    scheme: PedersenScheme = Depends(get_pedersen_scheme),
):
    """Generate a Pedersen commitment and range proof for wallet's score < threshold."""
    ...

@router.post("/scores/verify-threshold", response_model=VerifyResponse)
async def verify_threshold(body: VerifyRequest, scheme: PedersenScheme = Depends(get_pedersen_scheme)):
    """Verify a threshold proof. Returns {valid: bool}. Does not require authentication."""
    ...
```

### Serialisation

`ThresholdProof` must be serialisable to/from JSON (all integers as hex strings to avoid JavaScript integer overflow). `commitment_hex` is the hex encoding of `C`; the blinding factor `r` must never appear in API responses.

## Security Considerations

- **Blinding factor secrecy**: `r` must never appear in API responses, logs, or the `on_chain_submissions` audit table. The `ThresholdCommitResponse` contains only `commitment_hex` and the proof (which reveals no information about `r` under the Fiat-Shamir transform).
- **Group parameter integrity**: `PedersenParams` must be a compile-time constant, not loaded from the database or user input. A malicious caller substituting weak group parameters could break the binding property.
- **Score validation**: `commit()` must reject scores outside [0, 100] before any group operations.
- **Fiat-Shamir heuristic**: the challenge hash must include all public parameters (`C`, `threshold`, all bit commitments) to prevent selective forgery attacks. Use SHA-256 with a domain separator: `b"LedgerLens-Pedersen-v1"`.
- **Constant-time comparison**: `open()` must use `secrets.compare_digest()` for the commitment comparison to prevent timing side-channels.
- **Proof expiry**: `POST /scores/{wallet}/commit` returns a `valid_until` timestamp (1 hour). Expired proofs should be rejected by `POST /scores/verify-threshold`. Cache commitments in SQLite with expiry; do not cache the blinding factor.

## Testing Requirements

- **Unit — `commit()` deterministic**: same `(score, r)` always produces same `C`.
- **Unit — `open()` correct**: `open(commit(v, r), v, r)` returns True.
- **Unit — `open()` wrong score**: `open(commit(v, r), v+1, r)` returns False.
- **Unit — `open()` wrong randomness**: `open(commit(v, r), v, r+1)` returns False.
- **Unit — binding property check**: attempt to find `(score2, r2)` where `commit(score2, r2).C == commit(score1, r1).C` with different scores; assert this does not happen in 1000 random trials.
- **Unit — `prove_below_threshold()` soundness**: for `score=50, threshold=60`, proof is valid; for `score=60, threshold=60`, proof fails.
- **Unit — `verify_below_threshold()` completeness**: valid proof always verifies.
- **Unit — proof tamper resistance**: mutate one `range_response`; assert verification fails.
- **Unit — blinding factor not in response**: assert `r` does not appear in `ThresholdCommitResponse` JSON.
- **Integration — `POST /scores/{wallet}/commit` 200**: valid wallet, threshold=70 → 200 with `commitment_hex` and `proof`.
- **Integration — `POST /scores/verify-threshold`**: valid proof → `{"valid": true}`; mutated proof → `{"valid": false}`.
- **Integration — expired proof rejection**: mock clock past `valid_until`; assert `{"valid": false}`.

## Documentation Requirements

- Docstrings on all public methods of `PedersenScheme`.
- New file `docs/pedersen_commitments.md` covering: scheme overview, group parameter choices, threshold proof construction, Fiat-Shamir transform, and API usage examples.
- Update `README.md` Features section to mention privacy-preserving threshold attestation.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] `PedersenScheme`, `PedersenCommitment`, `PedersenParams`, `ThresholdProof` implemented in `detection/zk_commitment.py`.
- [ ] `commit()`, `open()`, `prove_below_threshold()`, `verify_below_threshold()` all implemented and correct.
- [ ] Blinding factor `r` never appears in API responses, logs, or any persistent store.
- [ ] Fiat-Shamir challenge uses domain separator and all public parameters.
- [ ] `POST /scores/{wallet}/commit` and `POST /scores/verify-threshold` operational.
- [ ] Proof expiry enforced by `verify-threshold`.
- [ ] All unit and integration tests pass; ≥90% branch coverage.
- [ ] `docs/pedersen_commitments.md` written.
- [ ] `README.md` and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have a solid foundation in applied cryptography — specifically discrete-log assumptions, Pedersen commitments, Sigma protocols, and the Fiat-Shamir heuristic. You understand the practical security requirements of implementing cryptographic primitives correctly in Python (constant-time operations, parameter validation, domain separation). Familiarity with ZK range proofs (bit decomposition or Bulletproofs) and their tradeoffs is highly valued. Experience shipping cryptographic code in production Python services is essential.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., applied cryptography, ZK proofs, Python security engineering).
2. **Relevant experience**: Pedersen commitments, Sigma protocols, or ZK range proofs you have implemented; any publications or open-source work.
3. **Approach / thoughts**: would you use a multiplicative group over a safe prime, or an elliptic curve (e.g., Ristretto255)? What are the practical tradeoffs for this use case?
4. **Estimated time**: realistic estimate to complete implementation, tests, and documentation to the Definition of Done standard.
