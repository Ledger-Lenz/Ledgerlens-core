# `zk_verifier` — Curve Arithmetic Cost Notes

This document explains the two design decisions in `contracts/zk_verifier/src/curve.rs` that
have the biggest effect on `verify_threshold`'s cost — Jacobian coordinates and the
binary-long-division `mul_mod` — and gives an honest, order-of-magnitude estimate of the
operation counts involved. It is referenced from `curve.rs`'s module doc comment.

**What this document is not:** a measured Soroban CPU-instruction or resource-fee number.
Producing that requires running the contract through `soroban-cli`'s cost simulator (or a
real/local testnet) and reading back the reported `cpu_insns`/`mem_bytes`, which is not
available in this environment. Everything below is a hand count of field operations, which is
the dominant cost driver but is not itself a gas figure. If precise gas numbers are needed,
run:

```bash
soroban contract invoke --id <deployed-id> --network testnet -- verify_threshold \
  --wallet <addr> --threshold <n> --proof <hex> --cost
```

and record the `cpu_insns` reported for a representative honest proof.

## Why Jacobian coordinates, not affine

Every elliptic-curve point addition/doubling in affine coordinates requires one modular
inverse (`inv_mod`, itself an `~254`-iteration `pow_mod` by Fermat's little theorem — far more
expensive than a single `mul_mod`). `verify_threshold` performs, per `NUM_BITS = 7` bit-proof,
four 256-bit scalar multiplications (`h.mul_scalar`, `bp.commit.mul_scalar` ×2, and one more in
the bit-sum reconstruction step), each of which is a double-and-add loop over ~256 bits — i.e.
on the order of 256 doublings plus up to 256 conditional additions *per scalar multiplication*.

Naively performed in affine coordinates, that is on the order of

```
7 bits × 4 scalar-mults/bit × ~384 point ops/scalar-mult (avg, MSB→LSB double-and-add)
  ≈ 10,700 point operations, each needing ≥1 modular inverse
  ≈ 10,700+ inversions ≈ 10,700 × ~254 mul_mods just for the inversions
```

— well into the millions of field multiplications, before counting the additions/doublings
themselves. That is not a viable per-call cost for a contract entrypoint.

Jacobian projective coordinates `(X, Y, Z)` represent the affine point as `(X/Z², Y/Z³)` and let
every doubling/addition be expressed as a fixed sequence of multiplications and squarings with
**no inversion at all**. Inversion is deferred to the handful of places an affine
representation is actually required:

- `to_affine()` — called once per `R0`/`R1`/`B` when building the Fiat-Shamir transcript
  (`append_point`), i.e. up to `3 × NUM_BITS = 21` times per `verify_threshold` call.
- `eq()` avoids inversion entirely for point-equality checks (cross-multiplies
  `X1·Z2² =? X2·Z1²` and `Y1·Z2³ =? Y2·Z1³` instead of normalizing both sides), so the final
  `accumulated.eq(&p_minus_t_g)` check and the `proof_data.score_commit.eq(&stored_p)` replay
  check cost zero inversions.

So the real inversion count for a full `verify_threshold` call is on the order of **21**
(transcript serialization), not 10,700+ — a reduction of roughly two and a half orders of
magnitude, which is the entire reason Jacobian coordinates were chosen over the simpler-to-read
affine formulas.

### Rough operation count for `verify_threshold`

Counting scalar multiplications (the dominant cost):

| Step | Scalar mults |
| --- | --- |
| Per bit-proof (`R0`, `R1` reconstruction): `h.mul_scalar(s0)`, `commit.mul_scalar(c0)`, `h.mul_scalar(s1)`, `b_minus_g.mul_scalar(c1)` | 4 × `NUM_BITS` (7) = 28 |
| Bit-sum reconstruction: `commit.mul_scalar(weight)` per bit | 1 × 7 = 7 |
| Threshold term: `g.mul_scalar(threshold)` | 1 |
| **Total** | **36 scalar multiplications** |

Each scalar multiplication is ~256 Jacobian doublings + up to 256 Jacobian additions (double-
and-add, MSB→LSB, current implementation does not use windowing/NAF). Using the standard
Jacobian formulas (`dbl-2009-l`: 4M+4S+1*2 ≈ 8 field ops; `add-2007-bl`: ~11M+5S ≈ 16 field ops,
where M/S are Fq multiplications/squarings, each itself one `mul_mod`):

```
36 scalar-mults × (256 doublings × ~8 + ~128 avg additions × ~16) field ops
  ≈ 36 × (2048 + 2048) ≈ 36 × 4096 ≈ 147,000 Fq multiplications
```

plus ~21 inversions × ~254 `mul_mod`s/inversion ≈ 5,300 more, plus `NUM_BITS + 2 = 9` SHA-256
calls (7 per-bit Fiat-Shamir challenges + 1 transcript-context hash + negligible other hashing).
**Order of magnitude: ~150,000 `mul_mod` calls per `verify_threshold` invocation.** This is an
analytical upper-bound-ish estimate (double-and-add without NAF is not the tightest possible
scheme), not a profiled number — see the disclaimer above for how to get an exact figure.

## Why binary long division for `mul_mod`, not Montgomery/Barrett

The original `mul_mod` used an unbounded `while remainder > 0` decrement loop — non-terminating
in the worst case and, even when it does terminate, with a gas cost that scales with the
*value* of the remainder rather than its bit-length, making worst-case cost unpredictable and
attacker-influenceable (an adversary choosing large operands could drive the loop count up).

`reduce_wide` replaces this with **bit-serial binary long division**: the 512-bit wide product
is reduced against the modulus one bit at a time, for a **fixed 512 iterations** regardless of
operand values. Each iteration is a shift, a compare, and a conditional subtract — all
constant-time-shaped (branches are on public data — proof bytes and the fixed modulus — not
secret key material, so this is a determinism/gas-predictability property, not a
side-channel-hardening one). This was chosen over Montgomery or Barrett reduction because:

- It requires no precomputed reduction constants tied to the modulus (Montgomery needs `R mod
  m`/`m' `; Barrett needs a precomputed `μ`), keeping the implementation small and avoiding a
  second modulus-specific constant to validate for `Fq` vs `Fr`.
- The issue's acceptance criteria explicitly sanction "Montgomery/Barrett/schoolbook-long-
  division-with-fixed-iterations" as equally acceptable; binary long division is the simplest
  of the three to verify by hand and against the 1000+-vector cross-language test
  (`mod_arith_vectors.txt`).
- Its cost is a flat, input-independent 512 iterations — trivial to reason about for gas
  budgeting, at the cost of being slower per-call than a tuned Montgomery reduction would be.
  If `verify_threshold`'s gas cost ever needs to be reduced further, switching `reduce_wide`'s
  internals to Montgomery form (keeping the same `U256`/`Fq`/`Fr` public API) is the natural
  next optimization, deferred here as it is not required to meet the issue's acceptance
  criteria and would add real complexity (values would need to be kept in Montgomery form
  end-to-end, including at the `from_be_bytes`/`to_be_bytes` wire boundary).

## Summary

- Jacobian coordinates cut inversions from ~10,700+ to ~21 per `verify_threshold` call —
  the single highest-leverage design decision in this contract.
- `mul_mod` is now a fixed-512-iteration binary long division: no unbounded loop, no
  value-dependent cost, terminates deterministically.
- Total cost is dominated by ~36 scalar multiplications (~150,000 `mul_mod` calls, order of
  magnitude) rather than by hashing or serialization.
- These are hand-counted estimates from reading the code, not measured Soroban gas. Get a real
  number via `soroban contract invoke ... --cost` against a deployed build before relying on
  this for a production gas budget.
