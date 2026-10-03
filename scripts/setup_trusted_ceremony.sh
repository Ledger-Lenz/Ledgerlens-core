#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Groth16 trusted-setup ceremony for the `score_range_proof` circuit.
# =============================================================================
#
# WHAT THIS SCRIPT DOES
#   Runs a full Groth16 trusted setup end to end: Phase 1 (Powers of Tau,
#   circuit-independent) followed by Phase 2 (circuit-specific), producing the
#   proving key (`<circuit>.zkey`) and verification key (`verification_key.json`)
#   that the zk-SNARK range-proof backend needs. See docs/zk_snark_range_proof.md
#   for the circuit design and the Sigma-protocol vs. zk-SNARK trade-offs.
#
# WHY THE CEREMONY EXISTS
#   Groth16 needs a structured reference string (SRS). Whoever generates the SRS
#   learns the secret "toxic waste" (tau, and the Phase-2 secrets). Anyone who
#   knows that toxic waste can forge proofs that verify against this key. The
#   ceremony's job is to make sure *nobody* ends up knowing it: each contributor
#   mixes in fresh secret randomness and then destroys it, so the setup is only
#   compromised if *every* contributor colluded or was compromised ("1-of-N"
#   honest-contributor assumption).
#
# SECURITY WARNING — this is a one-time, security-sensitive process.
#   * The `-e=` entropy strings below are PLACEHOLDERS and MUST NOT be used for
#     a real ceremony. Hard-coded, guessable, or reused entropy defeats the
#     entire point of the ceremony and lets an attacker reconstruct the toxic
#     waste. For production, omit `-e=` so snarkjs prompts for interactively
#     supplied entropy, run each contribution on a different machine/operator,
#     and treat every contributor's machine as needing to be clean.
#   * After a real ceremony, securely wipe intermediate `.ptau`/`.zkey` files
#     and any shell history / entropy material. Only `<circuit>.zkey` and
#     `verification_key.json` should survive, and both should be checksummed
#     (SHA-256) and their hashes committed — see docs/zk_snark_range_proof.md.
#   * Ordering matters: every step below consumes the output of the previous
#     one. Phase 2 cannot start until Phase 1 is finalized ("prepare phase2"),
#     and the circuit must be compiled to R1CS before `groth16 setup` can run.
#
# PREREQUISITES
#   * snarkjs installed globally (`npm i -g snarkjs`).
#   * circom installed and on PATH.
#
# NOTE ON CONSTRAINT SIZE
#   `powersoftau new bn128 12` builds an SRS supporting 2^12 constraints. If the
#   circuit grows beyond that, bump the power (e.g. 13/14/15) here AND everywhere
#   the `pot12_*` filenames appear, otherwise `groth16 setup` will fail.
#
# VERIFICATION & ATTESTATION (issue #946)
#   Every contribution is verified against the prior state before it is
#   accepted, and the final artifacts are published as versioned, checksummed
#   files with a signed transcript. Third parties can re-run the checks with
#   `scripts/verify_trusted_ceremony.sh` against the published artifacts.
# =============================================================================

CIRCUIT="score_range_proof"
CIRCUITS_DIR="circuits"
KEYS_DIR="circuits/keys"

# Versioned artifact directory for the published ceremony outputs.
CEREMONY_VERSION="${CEREMONY_VERSION:-v1}"
ARTIFACTS_DIR="circuits/ceremony/$CEREMONY_VERSION"
TRANSCRIPT="$ARTIFACTS_DIR/transcript.json"

# Output directory for the proving/verification keys produced by Phase 2.
mkdir -p "$KEYS_DIR" "$ARTIFACTS_DIR"

# -----------------------------------------------------------------------------
# 0. Attestation helpers.
#    `sha256sum` produces the checksums published alongside each artifact, and
#    `record_contribution` appends a verifiable entry to the ceremony transcript
#    (name, input hash, output hash) so the contribution chain can be audited.
# -----------------------------------------------------------------------------
sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

record_contribution() {
  local phase="$1" name="$2" input="$3" output="$4"
  local input_hash output_hash
  input_hash="$(sha256_of "$input")"
  output_hash="$(sha256_of "$output")"
  printf '{"phase":"%s","name":"%s","input_sha256":"%s","output_sha256":"%s"}\n' \
    "$phase" "$name" "$input_hash" "$output_hash" >> "$TRANSCRIPT"
  echo "  recorded $phase contribution '$name' ($output_hash)"
}

echo "=== Starting Trusted Setup Ceremony ($CEREMONY_VERSION) ==="
: > "$TRANSCRIPT"

# -----------------------------------------------------------------------------
# 1. Phase 1 — start a new Powers of Tau ceremony.
#    Produces `pot12_0000.ptau`: the empty, contribution-free SRS for the BN128
#    curve sized for 2^12 constraints. This transcript is circuit-independent
#    and could in principle be replaced by a well-known public Phase-1 ceremony
#    (e.g. Perpetual Powers of Tau / Hermez) instead of bootstrapping our own.
# -----------------------------------------------------------------------------
echo "Initializing Powers-of-Tau..."
snarkjs powersoftau new bn128 12 pot12_0000.ptau -v

# -----------------------------------------------------------------------------
# 2. Phase 1 — sequential MPC contributions (3 contributors).
#    Each `contribute` call folds the contributor's fresh secret randomness into
#    the SRS and writes a new `.ptau`. The chain is what gives the 1-of-N
#    guarantee: the setup stays secure as long as at least one of these
#    contributors was honest and actually destroyed their secret afterwards.
#
#    Each contribution is verified against the prior state with
#    `powersoftau verify` before it is accepted; a failed check aborts the
#    ceremony (set -e) so a bad contribution never enters the chain.
#
#    SECURITY: `-e=` supplies the entropy inline. The values here are dummy
#    strings for local/dev runs ONLY. For a real ceremony, drop `-e=` (snarkjs
#    will prompt), run each contribution as a separate operator on a separate
#    clean machine, and record each contribution's hash in the ceremony
#    transcript (docs/ceremony_transcripts/).
# -----------------------------------------------------------------------------
echo "Adding first contribution..."
snarkjs powersoftau contribute pot12_0000.ptau pot12_0001.ptau --name="Contributor 1" -v -e="random_entropy_source_1"
echo "Verifying first contribution..."
snarkjs powersoftau verify pot12_0001.ptau
record_contribution "phase1" "Contributor 1" pot12_0000.ptau pot12_0001.ptau

echo "Adding second contribution..."
snarkjs powersoftau contribute pot12_0001.ptau pot12_0002.ptau --name="Contributor 2" -v -e="random_entropy_source_2"
echo "Verifying second contribution..."
snarkjs powersoftau verify pot12_0002.ptau
record_contribution "phase1" "Contributor 2" pot12_0001.ptau pot12_0002.ptau

echo "Adding third contribution..."
snarkjs powersoftau contribute pot12_0002.ptau pot12_0003.ptau --name="Contributor 3" -v -e="random_entropy_source_3"
echo "Verifying third contribution..."
snarkjs powersoftau verify pot12_0003.ptau
record_contribution "phase1" "Contributor 3" pot12_0002.ptau pot12_0003.ptau

# -----------------------------------------------------------------------------
# 3. Phase 1 — finalize / "prepare phase 2".
#    Applies a random beacon and computes the Lagrange-basis evaluations, turning
#    the raw Powers of Tau into `pot12_final.ptau`, the universal SRS that
#    Phase 2 consumes. No further Phase-1 contributions are possible after this.
# -----------------------------------------------------------------------------
echo "Preparing Phase 2..."
snarkjs powersoftau prepare phase2 pot12_0003.ptau pot12_final.ptau -v

# -----------------------------------------------------------------------------
# 4. Compile the circuit to its constraint system.
#    circom emits:
#      * `<circuit>.r1cs` — the rank-1 constraint system (needed for `groth16
#        setup` in step 5),
#      * `<circuit>.wasm` — the witness generator used later at proving time,
#      * `<circuit>.sym`  — symbol/debug names.
#    This must run before Phase 2 because Groth16 setup is circuit-specific and
#    keys off the exact R1CS produced here — recompiling a changed circuit
#    invalidates any previously generated `.zkey`.
# -----------------------------------------------------------------------------
echo "Compiling circuit to r1cs..."
circom "$CIRCUITS_DIR/$CIRCUIT.circom" --r1cs --wasm --sym -o "$CIRCUITS_DIR"

# -----------------------------------------------------------------------------
# 5. Phase 2 — initial Groth16 setup.
#    Combines the circuit R1CS with the finalized Phase-1 SRS to produce the
#    first, contribution-free proving key `<circuit>_0000.zkey`. This key is NOT
#    safe to use yet: it still embeds Phase-2 toxic waste that must be washed out
#    by at least one Phase-2 contribution (step 6).
# -----------------------------------------------------------------------------
echo "Setting up Groth16..."
snarkjs groth16 setup "$CIRCUITS_DIR/$CIRCUIT.r1cs" pot12_final.ptau "$KEYS_DIR/${CIRCUIT}_0000.zkey"

# -----------------------------------------------------------------------------
# 6. Phase 2 — circuit-specific MPC contribution.
#    Folds fresh secret randomness into the proving key and writes the final
#    `<circuit>.zkey`. As with Phase 1, security rests on at least one Phase-2
#    contributor being honest and destroying their secret.
#
#    The contribution is verified against the R1CS and the finalized Phase-1
#    SRS with `zkey verify` before it is accepted; a failed check aborts the
#    ceremony so an invalid proving key is never published.
#
#    SECURITY: `-e="final_circuit_entropy"` is a placeholder. Use real,
#    interactively supplied entropy and, ideally, multiple independent Phase-2
#    contributors (repeat `zkey contribute` chaining the output files) for a
#    production ceremony.
# -----------------------------------------------------------------------------
echo "Contributing to Phase 2..."
snarkjs zkey contribute "$KEYS_DIR/${CIRCUIT}_0000.zkey" "$KEYS_DIR/$CIRCUIT.zkey" --name="Final Setup Contributor" -v -e="final_circuit_entropy"
echo "Verifying Phase 2 contribution..."
snarkjs zkey verify "$CIRCUITS_DIR/$CIRCUIT.r1cs" pot12_final.ptau "$KEYS_DIR/$CIRCUIT.zkey"
record_contribution "phase2" "Final Setup Contributor" "$KEYS_DIR/${CIRCUIT}_0000.zkey" "$KEYS_DIR/$CIRCUIT.zkey"

# -----------------------------------------------------------------------------
# 7. Export the verification key.
#    Extracts the public `verification_key.json` from the final proving key.
# -----------------------------------------------------------------------------
echo "Exporting verification key..."
snarkjs zkey export verificationkey "$KEYS_DIR/$CIRCUIT.zkey" "$KEYS_DIR/verification_key.json"

# -----------------------------------------------------------------------------
# 8. Publish versioned, checksummed attestation artifacts.
#    Copies the final proving key, verification key, and transcript into the
#    versioned artifact directory and writes a SHA-256 manifest. The manifest is
#    signed when a signing key is available (GPG or cosign), so downstream users
#    can confirm the artifacts were produced by this ceremony.
# -----------------------------------------------------------------------------
echo "Publishing attestation artifacts to $ARTIFACTS_DIR..."
cp "$KEYS_DIR/$CIRCUIT.zkey" "$ARTIFACTS_DIR/$CIRCUIT.zkey"
cp "$KEYS_DIR/verification_key.json" "$ARTIFACTS_DIR/verification_key.json"
cp "$CIRCUITS_DIR/$CIRCUIT.r1cs" "$ARTIFACTS_DIR/$CIRCUIT.r1cs"

(
  cd "$ARTIFACTS_DIR"
  sha256sum "$CIRCUIT.zkey" verification_key.json "$CIRCUIT.r1cs" transcript.json > SHA256SUMS
)

if [[ -n "${CEREMONY_SIGNING_KEY:-}" ]]; then
  if command -v gpg >/dev/null 2>&1; then
    echo "Signing SHA256SUMS with GPG key $CEREMONY_SIGNING_KEY..."
    gpg --batch --yes --local-user "$CEREMONY_SIGNING_KEY" --armor \
      --detach-sign --output "$ARTIFACTS_DIR/SHA256SUMS.asc" "$ARTIFACTS_DIR/SHA256SUMS"
  elif command -v cosign >/dev/null 2>&1; then
    echo "Signing SHA256SUMS with cosign..."
    cosign sign-blob --yes --key "$CEREMONY_SIGNING_KEY" \
      --output-signature "$ARTIFACTS_DIR/SHA256SUMS.sig" "$ARTIFACTS_DIR/SHA256SUMS"
  else
    echo "WARNING: CEREMONY_SIGNING_KEY set but neither gpg nor cosign is available; skipping signature." >&2
  fi
else
  echo "NOTE: set CEREMONY_SIGNING_KEY to sign the published SHA256SUMS manifest."
fi

echo "=== Ceremony complete. Artifacts: $ARTIFACTS_DIR ==="
echo "Third parties can audit with: scripts/verify_trusted_ceremony.sh $ARTIFACTS_DIR"
