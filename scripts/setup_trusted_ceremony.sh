#!/usr/bin/env bash
set -euo pipefail

# Scripts to run a Groth16 trusted setup ceremony (Phase 1 & Phase 2)
# requires snarkjs installed globally.

CIRCUIT="score_range_proof"
CIRCUITS_DIR="circuits"
KEYS_DIR="circuits/keys"

mkdir -p "$KEYS_DIR"

echo "=== Starting Trusted Setup Ceremony ==="

# 1. Start a new Powers-of-Tau ceremony (Phase 1)
echo "Initializing Powers-of-Tau..."
snarkjs powersoftau new bn128 12 pot12_0000.ptau -v

# 2. Add Contributions to Phase 1 (MPC with 3 contributors)
echo "Adding first contribution..."
snarkjs powersoftau contribute pot12_0000.ptau pot12_0001.ptau --name="Contributor 1" -v -e="random_entropy_source_1"

echo "Adding second contribution..."
snarkjs powersoftau contribute pot12_0001.ptau pot12_0002.ptau --name="Contributor 2" -v -e="random_entropy_source_2"

echo "Adding third contribution..."
snarkjs powersoftau contribute pot12_0002.ptau pot12_0003.ptau --name="Contributor 3" -v -e="random_entropy_source_3"

# 3. Prepare Phase 2
echo "Preparing Phase 2..."
snarkjs powersoftau prepare phase2 pot12_0003.ptau pot12_final.ptau -v

# 4. Generate R1CS constraint system from circom
echo "Compiling circuit to r1cs..."
circom "$CIRCUITS_DIR/$CIRCUIT.circom" --r1cs --wasm --sym -o "$CIRCUITS_DIR"

# 5. Setup Groth16 (Phase 2)
echo "Setting up Groth16..."
snarkjs groth16 setup "$CIRCUITS_DIR/$CIRCUIT.r1cs" pot12_final.ptau "$KEYS_DIR/${CIRCUIT}_0000.zkey"

# 6. Contribute to Phase 2
echo "Contributing to Phase 2..."
snarkjs zkey contribute "$KEYS_DIR/${CIRCUIT}_0000.zkey" "$KEYS_DIR/$CIRCUIT.zkey" --name="Final Setup Contributor" -v -e="final_circuit_entropy"

# 7. Export verification key
echo "Exporting verification key..."
snarkjs zkey export verificationkey "$KEYS_DIR/$CIRCUIT.zkey" "$KEYS_DIR/verification_key.json"

# 8. Clean up intermediate ceremony files
echo "Cleaning up temporary files..."
rm pot12_*.ptau "$KEYS_DIR/${CIRCUIT}_0000.zkey"

echo "=== Trusted Setup Ceremony Completed ==="
echo "Keys generated at:"
echo " - $KEYS_DIR/$CIRCUIT.zkey"
echo " - $KEYS_DIR/verification_key.json"
