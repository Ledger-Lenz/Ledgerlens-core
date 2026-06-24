---
title: "Add HMAC Integrity Verification for Ingested EVM Bridge Events"
labels: ["difficulty: advanced", "area: ingestion", "type: security"]
assignees: []
---

## Summary
`ingestion/bridge_loader.py` ingests bridge transfer events from Allbridge relayer contracts without verifying the cryptographic integrity of those events against the on-chain contract's emitted log data. An adversary who controls a malicious RPC endpoint or a man-in-the-middle position could inject fabricated bridge events that would cause LedgerLens to produce false wash-trading alerts against innocent wallets. Adding HMAC-based integrity verification — matching ingested event data against a trusted relayer contract signature or a locally computed on-chain event log hash — will ensure that only authentic bridge events influence the detection pipeline.

## Background & Context
The cross-chain detection feature (see `docs/cross_chain_detection.md`) links Stellar wallets to EVM counterparts via Allbridge bridge events. These events are fetched from EVM chains by `ingestion/evm_loader.py` (raw logs) and decoded by `ingestion/bridge_loader.py` (into `BridgeEvent` records). They then feed six cross-chain ML features that contribute to the `LedgerLens Risk Score`.

The trust model for EVM event logs is:
1. **On-chain events are authoritative**: events emitted by the Allbridge contract on Ethereum/Base/Polygon are cryptographically committed to the block via the Merkle Patricia Trie. Any tampering changes the block hash.
2. **RPC providers are untrusted intermediaries**: the JSON-RPC provider (Infura, Alchemy, or a self-hosted node) can return fraudulent log data if compromised. Standard EVM clients do not cryptographically verify log data returned by `eth_getLogs` — this is a known limitation of the JSON-RPC API.
3. **HMAC over canonical event fields**: while full Merkle proof verification (using `eth_getProof`) is complex, a simpler defence is to compute an HMAC over the canonical event fields (address, topics, data, blockHash, transactionHash, logIndex) and verify this HMAC against a trusted key — or, more practically, to verify the `transactionHash` against the on-chain receipt using `eth_getTransactionReceipt` to confirm the log was genuinely included.

This issue implements two layers of integrity verification:
1. **Receipt confirmation**: for each bridge event, call `eth_getTransactionReceipt` and verify that the log at `logIndex` in the receipt matches the event fetched via `eth_getLogs`.
2. **Canonical event hash**: compute a canonical hash of the verified event fields and store it as a tamper-detection fingerprint alongside the event.

## Objectives
- [ ] Implement `BridgeEventVerifier` in `ingestion/bridge_loader.py` that performs receipt-based log verification: for each `BridgeEvent`, call `eth_getTransactionReceipt` and confirm the log fields match.
- [ ] Implement canonical event hash computation (`compute_canonical_event_hash`) that produces a deterministic SHA-256 hash of the event's immutable fields, stored in the `bridge_events` table as `canonical_hash`.
- [ ] Add a `verification_status` field to the `BridgeEvent` model with values `verified`, `unverified` (receipt check skipped for performance), and `tampered` (receipt check failed — event rejected).
- [ ] Implement a configurable `BRIDGE_VERIFY_SAMPLE_RATE` so operators can choose between full verification (100%), statistical sampling (e.g., 10%), or disabled (0%) to balance security vs API cost.

## Technical Requirements

**Receipt-based log verification algorithm:**
```python
async def verify_event_via_receipt(
    self,
    event: BridgeEvent,
    provider_pool: EVMProviderPool,
) -> VerificationResult:
    """
    1. Call eth_getTransactionReceipt for event.tx_hash
    2. Locate the log at event.log_index in receipt.logs
    3. Compare: address, topics, data, blockHash, transactionIndex
    4. Return VerificationResult.VERIFIED if all match, else TAMPERED
    """
    receipt = await provider_pool.call(
        event.chain_id, "eth_getTransactionReceipt", [event.tx_hash]
    )
    if receipt is None:
        return VerificationResult.RECEIPT_NOT_FOUND

    logs = receipt.get("logs", [])
    if event.log_index >= len(logs):
        return VerificationResult.LOG_INDEX_OUT_OF_RANGE

    receipt_log = logs[event.log_index]
    if (
        receipt_log["address"].lower() == event.contract_address.lower()
        and receipt_log["topics"] == event.topics
        and receipt_log["data"] == event.data
        and receipt_log["blockHash"] == event.block_hash
    ):
        return VerificationResult.VERIFIED
    return VerificationResult.TAMPERED
```

**`VerificationResult` enum:**
```python
class VerificationResult(str, Enum):
    VERIFIED = "verified"
    TAMPERED = "tampered"
    RECEIPT_NOT_FOUND = "receipt_not_found"   # tx not yet mined or pruned
    LOG_INDEX_OUT_OF_RANGE = "log_index_out_of_range"
    SKIPPED = "skipped"    # not selected for sampling
    DISABLED = "disabled"  # verification turned off
```

**Canonical event hash computation:**
```python
import hashlib, json

def compute_canonical_event_hash(
    chain_id: int,
    contract_address: str,
    topics: list[str],
    data: str,
    block_number: int,
    tx_hash: str,
    log_index: int,
) -> str:
    """
    Deterministic SHA-256 of the canonical event fields.
    All hex strings normalised to lowercase.
    Used as a tamper-detection fingerprint stored alongside the event.
    """
    canonical = {
        "chain_id": chain_id,
        "address": contract_address.lower(),
        "topics": [t.lower() for t in topics],
        "data": data.lower(),
        "block_number": block_number,
        "tx_hash": tx_hash.lower(),
        "log_index": log_index,
    }
    return hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
```

**`BridgeEvent` model extension:**
```python
class BridgeEvent(BaseModel):
    # ... existing fields ...
    canonical_hash: str | None = None          # populated by BridgeEventVerifier
    verification_status: VerificationResult = VerificationResult.DISABLED
    verified_at: datetime | None = None
```

**SQLite schema for `bridge_events` table update:**
```sql
ALTER TABLE bridge_events ADD COLUMN canonical_hash TEXT;
ALTER TABLE bridge_events ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'disabled';
ALTER TABLE bridge_events ADD COLUMN verified_at TIMESTAMP;
```
(This ALTER TABLE must be wrapped in the database migration system — see `cli.py db-migrate`.)

**Sampling logic in `bridge_loader.py`:**
```python
import random

sample_rate = settings.BRIDGE_VERIFY_SAMPLE_RATE

async def _process_event(self, event: BridgeEvent) -> None:
    event.canonical_hash = compute_canonical_event_hash(...)
    if sample_rate > 0 and random.random() < sample_rate:
        result = await self._verifier.verify_event_via_receipt(event, self._provider_pool)
        event.verification_status = VerificationResult(result.value)
        event.verified_at = datetime.utcnow()
        if result == VerificationResult.TAMPERED:
            logger.error(
                "TAMPERED bridge event detected: tx=%s log_index=%d chain=%d",
                event.tx_hash, event.log_index, event.chain_id
            )
            # Send to DLQ with error_class=SCHEMA_ERROR and do NOT write to main table
            self._dlq.enqueue("bridge_loader", TamperedEventError(event), event.raw_bytes)
            return
    else:
        event.verification_status = VerificationResult.SKIPPED
    self._write_event(event)
```

**Tampered event policy**: tampered events must be:
1. Sent to the DLQ (ISSUE-014) with `error_class=SCHEMA_ERROR`
2. Logged at `ERROR` with the transaction hash and chain ID
3. Counted in `IngestionMetricsCollector.events_tampered_total` (add this counter in ISSUE-015)
4. NOT written to the `bridge_events` table

**Configuration** (add to `config/settings.py`):
- `BRIDGE_VERIFY_SAMPLE_RATE`: default `1.0` (100% — verify all events)
- `BRIDGE_VERIFY_RECEIPT_TIMEOUT_SECONDS`: default `10.0`

## Security Considerations
- The HMAC/hash approach here is **not** a full Merkle proof — it relies on the `eth_getTransactionReceipt` call going to the same (or a different) trusted provider. For maximum security, operators should configure `EVMProviderPool` (ISSUE-013) with multiple independent providers so a tampering attack would need to compromise all of them simultaneously.
- `BRIDGE_VERIFY_SAMPLE_RATE=0.0` disables all verification and must emit a `WARNING` log on startup: `"Bridge event verification disabled — cross-chain integrity not guaranteed"`.
- The canonical hash stored in the database must be computed before writing — not read back from the response — to ensure it reflects what LedgerLens ingested, not what a subsequent attacker might claim.
- `TamperedEventError` messages must not include the full event `data` field (which can be large and potentially contain sensitive encoded addresses) — include only the transaction hash, log index, and chain ID.
- Timing: `eth_getTransactionReceipt` calls must have a configurable timeout and must not block the ingestion pipeline indefinitely. Use `asyncio.wait_for` with `BRIDGE_VERIFY_RECEIPT_TIMEOUT_SECONDS`.

## Testing Requirements
- Unit tests covering `compute_canonical_event_hash()`: deterministic output for same inputs, different `log_index` → different hash, case-insensitive for hex strings (uppercase and lowercase produce same hash)
- Unit tests covering `BridgeEventVerifier.verify_event_via_receipt()`: matching log → `VERIFIED`, mismatched `data` field → `TAMPERED`, receipt not found → `RECEIPT_NOT_FOUND`, log index out of range → `LOG_INDEX_OUT_OF_RANGE`
- Unit tests covering sampling: with `sample_rate=0.0`, no receipts fetched; with `sample_rate=1.0`, all events verified; with `sample_rate=0.5`, ~50% verified (statistical test with large N)
- Unit tests covering tampered event handling: tampered event goes to DLQ, not to `bridge_events` table
- Integration tests: mock `eth_getTransactionReceipt` returning matching data; assert event written with `verification_status=verified`
- Integration tests: mock `eth_getTransactionReceipt` returning mismatched `data`; assert event in DLQ, not in `bridge_events`
- Edge cases: receipt with empty `logs` list, `eth_getTransactionReceipt` timeout, `canonical_hash` collision (extremely unlikely but the code must handle it — unique constraint violation → update `verification_status` on existing row)
- Performance benchmark: verifying 1,000 bridge events with `sample_rate=1.0` against a mock provider should complete in < 30 seconds

## Documentation Requirements
- Update `docs/cross_chain_detection.md` with a section on bridge event integrity verification, the receipt-confirmation approach, and the Merkle proof limitation
- Add docstrings to `BridgeEventVerifier`, `compute_canonical_event_hash`, and `_process_event`
- Update `README.md` Security Notes section (under Webhook Alerts) with a brief mention of bridge event verification
- Document the `BRIDGE_VERIFY_SAMPLE_RATE` configuration trade-off (security vs API call cost) in `config/settings.py`

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: EVM transaction receipts, `eth_getTransactionReceipt`, event log verification, HMAC/SHA-256, Allbridge bridge event schema
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Security-focused engineer with deep experience in EVM transaction verification. Understanding of EVM log encoding (topics, data ABI encoding), `eth_getTransactionReceipt` response structure, and the security model of JSON-RPC event logs. Python async and cryptographic hashing experience is essential.
