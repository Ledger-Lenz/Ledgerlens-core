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

## `ledgerlens-score` ABI Integration

The aggregator targets the current ten-argument `submit_score` ABI:

```text
submit_score(
  signers, wallet, asset_pair, score, benford_flag, ml_flag,
  timestamp, confidence, model_version, attestation_input
)
```

Oracle nodes sign every caller-controlled score field in the
`LedgerLens-Oracle-v2` canonical message. `asset_pair` is encoded as a Soroban
`Symbol` XDR value, matching the destination ABI; callers must provide a
non-empty ASCII alphanumeric/underscore identifier of at most 32 bytes.

The aggregator derives the remaining arguments rather than accepting
caller-controlled authorization data:

- `signers = [oracle_aggregator contract address]`
- `attestation_input = None`

Deployment must configure the aggregator contract address as the
`ledgerlens-score` service address. Before invoking `submit_score`, the
aggregator authorizes that exact nested call with
`authorize_as_current_contract`, so the destination's `require_auth` check is
preserved. The destination's optional secp256k1 service-pubkey attestation must
not be enabled for this service path because the Ed25519 oracle quorum provides
the cryptographic attestation.

A destination contract error or trap propagates and rolls back the aggregator
invocation. It is deliberately not converted to `false`: `false` means quorum
or replay rejection, while a target trap means quorum succeeded but persistence
failed.

## Key Management
- Each node's ED25519 private key is injected via environment variables (`ORACLE_NODE_{1..5}_KEY`).
- Keys are never logged or stored on disk unencrypted.
- The `/admin/oracle/status` endpoint exposes only public keys and health status.

## Deployment & Initialization (Access Control)

`initialize` is now access-controlled. It takes a `deployer: Address` parameter and
calls `deployer.require_auth()`, so only the identity that **authorises** the call can
set the trusted oracle key set, quorum threshold, and score-contract address. Because
re-initialization is permanently blocked, the first successful `initialize` fixes the
configuration for the contract's lifetime — there is no recovery short of redeployment.

### Front-running window

Soroban deployment and `initialize` are separate transactions. Anyone who submits a
validly-authorised `initialize` *first* wins. To close this window as tightly as
operationally possible:

1. **Generate a dedicated deployer key** for the contract (do **not** reuse an oracle
   node key or a hot wallet).
2. **Deploy and initialize atomically**: broadcast the deploy transaction and the
   `initialize(deployer, threshold, oracle_keys, score_contract)` call back-to-back,
   with the `deployer` signing the `initialize` invocation. Prefer submitting both
   within the same operational batch / immediately after deployment, and monitor the
   ledger so a competing `initialize` is detected before any score is accepted.
3. **Verify** the recorded authority after deployment:
   `get_deployer()` returns the `deployer` address that authorised initialization, so
   operators can confirm the expected identity controls configuration.

### Why not bake the deployer in at construction?

Soroban has no constructor; configuration is performed via the post-deploy
`initialize` call. The chosen design accepts the calling `deployer` as authoritative
for this one-time call (consistent with `zk_verifier`'s `submit_score(admin, …)` admin
pattern elsewhere in the codebase) and records it via `get_deployer()` for auditability.
The residual front-running risk is mitigated operationally (deploy + init tightly
coupled), not cryptographically — tracked separately for `zk_verifier`.

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
