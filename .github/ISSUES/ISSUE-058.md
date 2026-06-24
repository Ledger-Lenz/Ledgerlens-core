---
title: "Implement Secure Multi-Party Computation Protocol for Privacy-Preserving Gradient Exchange"
labels: ["difficulty: expert", "area: federated-learning", "type: feature"]
assignees: []
---

## Summary
The current federated aggregation server receives plaintext gradient updates from participating exchange clients, requiring exchanges to trust the central aggregator with their raw model gradients. Replacing plaintext gradient exchange with a secure multi-party computation (SMPC) protocol — specifically additive secret sharing — ensures that no single party, including the aggregator, can reconstruct any client's individual gradient vector.

## Background & Context
`federated/aggregation_server.py` implements Byzantine-fault-tolerant aggregation (Krum/Multi-Krum) over plaintext gradients. For exchanges operating under strict data-sharing regulations, transmitting gradients in plaintext may leak proprietary trading pattern information embedded in those gradients. SMPC via additive secret sharing allows each client to split their gradient into N shares and distribute one share to each of N aggregation servers; any subset below a threshold cannot reconstruct the original.

This builds on the BFT aggregation server (ISSUE-057) and differential privacy layer (ISSUE-070), completing the privacy stack for the federated learning subsystem.

## Objectives
- [ ] Implement additive secret sharing over gradient tensors in `federated/smpc.py`
- [ ] Modify `federated/fl_client.py` to split gradients into shares before transmission
- [ ] Extend `federated/aggregation_server.py` to aggregate shares from multiple aggregators before reconstruction
- [ ] Add share-commitment scheme (hash of each share) so clients can verify their shares were included
- [ ] Write integration test simulating 3 clients, 3 aggregators, threshold=2

## Technical Requirements
Use a 2-of-3 additive secret sharing scheme: `share_1 + share_2 + share_3 = gradient` (mod large prime). Each exchange sends share_i to aggregator_i. Aggregators exchange partial sums; reconstruction requires any 2 of 3 partial sums. Library: `python-secretsharing` or implement directly with `numpy` for float tensors.

## Definition of Done
- [ ] No single aggregator can reconstruct any client's gradient from its share alone
- [ ] Aggregated model matches plaintext aggregation result (within floating-point tolerance)
- [ ] Integration test with simulated Byzantine aggregator passes
- [ ] Performance overhead vs plaintext < 2× for 10k-parameter gradient vectors

## For Contributors
Cryptography background required. Experience with MPC frameworks (MP-SPDZ, CrypTen, or manual secret sharing) strongly preferred.
