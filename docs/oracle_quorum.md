# Multi-Signature Oracle Quorum

Ledgerlens-core utilizes a 3-of-5 multi-signature oracle quorum to secure on-chain RiskScore submissions, eliminating single points of failure and preventing single-key compromise attacks. For a consolidated system threat model, see the [STRIDE Threat Model](threat_model.md).

## Architecture Overview

Instead of relying on a single publisher key, the system distributes trust across `n=5` independent Oracle Nodes.
Each node evaluates the wash-trading algorithms, computes the risk score, and signs a canonical message using its own ED25519 private key.

The `OracleCoordinator` aggregates these signatures. Once `k=3` valid signatures are gathered, it constructs a `QuorumSignature` and invokes the `oracle_aggregator` Soroban smart contract.

The `oracle_aggregator` contract:
1. Reconstructs the canonical message.
2. Verifies each ED25519 signature against the authorized oracle public keys.
3. Ensures at least `k` valid signatures are present.
4. Checks timestamps for replay protection.
5. Forwards the approved score to the main `ledgerlens-score` contract.

## Key Management
- Each node's ED25519 private key is injected via environment variables (`ORACLE_NODE_{1..5}_KEY`).
- Keys are never logged or stored on disk unencrypted.
- The `/admin/oracle/status` endpoint exposes only public keys and health status.

## Threshold Selection Guidance
- With `n=5` and `k=3`, the network tolerates 2 simultaneous node failures without losing liveness.
- It also tolerates up to 2 compromised nodes without allowing malicious score submissions.

## Key Rotation Procedure
To rotate keys or alter the quorum threshold:
1. Generate new ED25519 keypairs.
2. Since the contract cannot be re-initialized without upgrading, an authorized administrator must invoke an update mechanism or redeploy the contract. *(Currently, initialization happens once, so key rotation requires a contract redeployment or an upgrade proposal, see ISSUE-070)*.

## Failure Mode Analysis
- **1-2 Nodes Offline:** Quorum still achieved (3 remaining nodes can sign).
- **3+ Nodes Offline:** Quorum cannot be reached. Submissions pause until nodes recover.
- **Node Compromise (1-2):** Attacker cannot submit forged scores as the contract demands 3 valid signatures.
- **Node Desync:** Replay protection (`timestamp`) forces scores to be submitted within 5 minutes. Nodes must be synchronized via NTP.
