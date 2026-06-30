# Cross-Chain Detection

LedgerLens extends its wash-trading detection to cover activity that spans the Stellar DEX and EVM-compatible chains (Ethereum, Base, Polygon). Wallets that bridge assets back and forth between Stellar and EVM networks to launder wash-trade proceeds are now detected and factored into the risk score.

## Architecture

```
EVM RPC (eth_getLogs)
        │
        ▼
┌───────────────────┐        ┌──────────────────────────┐
│  EVMTradeLoader   │        │   BridgeTransferLoader   │
│  (evm_loader.py)  │        │   (bridge_loader.py)     │
│                   │        │                          │
│ Uniswap V2/V3     │        │ Allbridge TokensSent     │
│ Swap events       │        │ bytes32 → G-address      │
└─────────┬─────────┘        └────────────┬─────────────┘
          │                               │
          │   CrossChainTrade             │   BridgeTransfer
          ▼                               ▼
┌─────────────────────────────────────────────────────┐
│                  SQLite (bridge_transfers)           │
└─────────────────────────┬───────────────────────────┘
                          │
                          ▼
               ┌──────────────────────┐
               │  CrossChainLinker    │
               │  (cross_chain_       │
               │   linker.py)         │
               │                      │
               │  link_wallets()      │
               │  get_evm_trade_      │
               │  pattern()           │
               └──────────┬───────────┘
                          │
                          ▼
               ┌──────────────────────┐
               │ feature_engineering  │
               │                      │
               │ build_cross_chain_   │
               │ features()           │
               └──────────┬───────────┘
                          │
                          ▼
               ┌──────────────────────┐
               │   Risk Score (0–100) │
               │   + cross_chain_     │
               │     links field      │
               └──────────────────────┘
```

## New Modules

### `ingestion/evm_loader.py`

Fetches and parses Uniswap V2 and V3 swap events from EVM-compatible chains.

**Key classes:**
- `EVMTradeLoader(chain, rpc_url, pool_addresses)` — fetch swaps from specific pools
- `CrossChainTrade` — Pydantic model for a parsed EVM swap

**Rate limiting:** token-bucket at 10 RPS per chain with exponential backoff on HTTP 429.

**Address validation:** all pool addresses are converted to EIP-55 checksummed form at construction time; malformed addresses raise `ValueError` immediately.

**Swap event topics:**
- Uniswap V3: `Swap(address indexed sender, address indexed recipient, int256 amount0, int256 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick)`
- Uniswap V2: `Swap(address indexed sender, uint256 amount0In, uint256 amount1Out, uint256 amount1In, uint256 amount0Out, address indexed to)`

Amount direction convention (V3): from the **pool's** perspective — `amount0 < 0` means the pool paid out token0 (user received token0, paid token1), so `token_in = token1`, `token_out = token0`.

### `ingestion/bridge_loader.py`

Fetches and parses Allbridge `TokensSent` events from EVM chains. These events record EVM→Stellar bridge transfers and encode the Stellar recipient as a raw 32-byte ed25519 public key.

**Key class:** `BridgeTransferLoader(chain, rpc_url, contract_address)`

**`decode_bytes32_to_stellar(bytes)`** converts the 32-byte ed25519 public key to a Stellar G-address using `stellar_sdk.Keypair.from_raw_ed25519_public_key`.

**Allbridge event ABI:**
```
TokensSent(address indexed sender, bytes32 recipient, uint256 amount)
```
- `topics[1]`: sender (EVM wallet, indexed, zero-padded)
- `data[0:32]`: recipient bytes32 (raw ed25519 public key)
- `data[32:64]`: amount uint256

### `detection/cross_chain_linker.py`

Links Stellar wallets to their EVM counterparts and computes EVM-side trading statistics.

**`CrossChainLinker(db_path=None, min_confidence=0.70)`**

- `link_wallets(stellar_wallet, lookback_days=90)` — returns EVM wallet addresses that bridged with the Stellar wallet within the lookback window
- `get_evm_trade_pattern(evm_wallets, chain, evm_trades=None, db_path=None)` — returns a dict with:
  - `round_trip_frequency` — fraction of outbound bridge transfers that have a matching inbound within 24h (wash-trading signal)
  - `total_evm_volume` — total USD volume from the provided `evm_trades` list
  - `unique_counterparties` — distinct counterparty addresses seen
  - `benford_mad` — Benford MAD on the trade amounts
- `score_hypothesis(stellar_wallet, evm_wallet, bridge_events)` — compute Bayesian confidence score for the link hypothesis (returns `WalletLinkHypothesis`)
- `persist_hypothesis(hypothesis)` — persist accepted hypotheses (confidence >= 0.7) to SQLite
- `get_accepted_links(stellar_wallet, min_confidence=None)` — retrieve accepted link hypotheses sorted by confidence descending

## Seven Cross-Chain Features

These features are appended to the end of `FEATURE_NAMES` (backward-compatible; existing model scores are unchanged until a retrain includes EVM data).

| Feature | Description | Wash-trade signal |
|---|---|---|
| `has_evm_link` | 1.0 if any EVM bridge transfers exist within 90 days, else 0.0 | Presence of cross-chain activity |
| `evm_round_trip_frequency` | Fraction of EVM→Stellar outbound transfers with a matching Stellar→EVM inbound within 24h | High = funds go out and come back quickly |
| `evm_benford_mad_30d` | Benford MAD on EVM trade amounts (30d) | Digit anomalies in EVM swap amounts |
| `evm_counterparty_concentration` | HHI of counterparty addresses in EVM trades (0=diverse, 1=monopoly) | High = trading with very few counterparties |
| `bridge_volume_ratio` | EVM bridge volume / (Stellar SDEX volume + EVM bridge volume) | High = activity concentrated on bridge |
| `cross_chain_time_lag_median_h` | Median hours between paired EVM and Stellar trades | Very low = near-instant round-trips |
| `cross_chain_round_trip_score` | Correlation score (0–1) for Stellar→EVM→Stellar round-trip bridge patterns based on amount similarity (within 5%), timing proximity (within 24h), and intermediate hops | High = strong evidence of multi-network wash cycles |

## API Changes

### `GET /scores/{wallet}`

Response now includes a `cross_chain_links` field:

```json
{
  "scores": [
    {
      "wallet": "GABC...",
      "score": 85,
      ...
    }
  ],
  "cross_chain_links": ["0xAb5801...", "0xCBCd..."]
}
```

### `GET /wallets/{wallet}/cross-chain`

New endpoint returning bridge transfer history for a Stellar wallet:

```json
[
  {
    "chain": "ethereum",
    "direction": "evm_to_stellar",
    "evm_wallet": "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B",
    "stellar_wallet": "GABC...",
    "amount_usd": null,
    "token": "USDC",
    "tx_hash_evm": "0xabab...",
    "tx_hash_stellar": null,
    "timestamp": "2026-06-19T00:00:00Z"
  }
]
```

Note: `amount_usd` is labeled as an estimate (or `null` when unavailable) — never used for accounting.

## Database Schema (migration 7)

```sql
CREATE TABLE IF NOT EXISTS bridge_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    direction TEXT NOT NULL,
    evm_wallet TEXT NOT NULL,
    stellar_wallet TEXT NOT NULL,
    amount_usd REAL,
    token TEXT NOT NULL,
    tx_hash_evm TEXT NOT NULL,
    tx_hash_stellar TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bridge_stellar ON bridge_transfers(stellar_wallet, timestamp);
CREATE INDEX IF NOT EXISTS idx_bridge_evm ON bridge_transfers(evm_wallet, timestamp);
```

## Security Notes

- **EIP-55 address validation**: all EVM addresses are validated at construction time. Malformed or non-checksummed addresses raise `ValueError` before any I/O occurs.
- **No RPC URLs in responses**: RPC endpoints are read from environment variables and never included in API responses.
- **USD estimates**: `amount_usd` is explicitly labeled as an estimate and is `null` when unavailable. It is never used for accounting or financial calculation.
- **`EVM_POOL_ADDRESSES` validated at startup**: `Settings.__post_init__` validates every address in the comma-separated list. A misconfigured address fails at startup, not silently at runtime.

## Configuration

See `.env.example` for the full list of EVM settings. Required variables:

| Variable | Description |
|---|---|
| `EVM_RPC_ETHEREUM` | JSON-RPC endpoint for Ethereum mainnet (legacy single-provider) |
| `EVM_RPC_BASE` | JSON-RPC endpoint for Base (legacy single-provider) |
| `EVM_RPC_POLYGON` | JSON-RPC endpoint for Polygon (legacy single-provider) |
| `EVM_LOOKBACK_BLOCKS` | Number of blocks to look back when fetching events (default: 7200 ≈ 24h) |
| `EVM_POOL_ADDRESSES` | Comma-separated list of EIP-55 checksummed pool addresses to monitor |

### Multi-Provider Failover (EVMProviderPool)

`EVMProviderPool` replaces the single-endpoint model with a prioritised list of JSON-RPC providers per chain. When the primary provider fails, the pool automatically retries on the next healthy provider without any manual intervention. This eliminates the cross-chain ingestion gap that occurred during provider maintenance windows, rate-limit spikes, or regional outages.

#### Provider Configuration

Set `EVM_PROVIDERS` to a JSON array of provider objects. Each entry requires `chain_id` (int), `rpc_url` (**must be `https://`**), and `name` (string). Optional fields: `priority` (int, lower = tried first, default 0) and `max_requests_per_second` (float, default 10.0).

```bash
EVM_PROVIDERS=[
  {"chain_id": 1, "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY", "name": "infura", "priority": 0},
  {"chain_id": 1, "rpc_url": "https://eth-mainnet.alchemyapi.io/v2/YOUR_KEY", "name": "alchemy", "priority": 1},
  {"chain_id": 8453, "rpc_url": "https://base-mainnet.infura.io/v3/YOUR_KEY", "name": "infura-base", "priority": 0},
  {"chain_id": 137, "rpc_url": "https://polygon-mainnet.infura.io/v3/YOUR_KEY", "name": "infura-polygon", "priority": 0}
]
```

When `EVM_PROVIDERS` is empty or unset (`[]`), the pool falls back to the legacy `EVM_RPC_ETHEREUM` / `EVM_RPC_BASE` / `EVM_RPC_POLYGON` single-endpoint settings.

| Variable | Default | Description |
|---|---|---|
| `EVM_PROVIDERS` | `[]` | JSON array of provider objects (see above) |
| `EVM_MAX_BLOCK_LAG` | `10` | Blocks behind chain head before health degrades; triggers lag alert when all providers exceed this |
| `EVM_PROBE_INTERVAL_SECONDS` | `15.0` | Seconds between `eth_blockNumber` health probe cycles |
| `EVM_CIRCUIT_BREAKER_THRESHOLD` | `5` | Consecutive failures before a provider's circuit opens |

#### Failover Behaviour

On each `eth_getLogs` / `eth_blockNumber` / `eth_getTransactionReceipt` call, the pool selects providers in order of a composite **health score**:

```
provider_score = health_score - max(0, reference_block - current_block) * 0.1
```

Where `reference_block` is the highest `current_block` seen across all providers for that chain. Each block behind the estimated chain head costs 0.1 health points. Circuit-open providers are always scored at −1.0 and skipped entirely.

If a call fails (timeout or JSON-RPC error):
- The failed provider's `health_score` decreases by 0.2
- `consecutive_failures` increments
- Once `consecutive_failures >= EVM_CIRCUIT_BREAKER_THRESHOLD`, the circuit opens and the provider is bypassed

A successful call restores 0.05 health per call (capped at 1.0) and resets `consecutive_failures` to 0.

If every provider for a chain is exhausted, `EVMProviderPoolExhaustedError` is raised — bridge ingestion for that chain pauses until the next probe cycle recovers at least one provider.

#### Health Probing

A background asyncio task polls every provider's `eth_blockNumber` every `EVM_PROBE_INTERVAL_SECONDS` seconds. On a successful probe:
- `current_block` and `last_probe_at` are updated
- If the provider's circuit was open **and** `consecutive_failures == 0` (cleared by a prior successful `call()`), the circuit resets automatically

#### Block-Lag Monitoring

After each probe cycle, the pool computes the reference block (maximum `current_block` across all providers per chain). If **all** providers for a chain are more than `EVM_MAX_BLOCK_LAG` blocks behind the reference, a `WARNING` is emitted and the chain ID is added to `EVMProviderPoolStats.chains_with_lag_alert`. This distinguishes the "all providers old news" scenario (which causes missed events) from a single lagging provider (which is handled silently by routing to others).

Inspect lag alerts via `pool.stats.chains_with_lag_alert` or the Prometheus metrics layer.

#### API Key Security

All `rpc_url` values must use `https://`. The `http://` scheme is rejected at startup with a `ValueError` because HTTP transmits API keys in plaintext — a passive network observer would be able to exfiltrate your Infura or Alchemy credentials.

API keys embedded in URLs (e.g. `infura.io/v3/SECRET`) are **masked** in all log output. `EVMProvider.__repr__` replaces the key segment with `***`. `EVMProviderPoolExhaustedError` messages contain only provider names and chain IDs — never URLs.

## Bridge Event Integrity Verification

### Threat model

Bridge events are fetched from EVM chains via JSON-RPC (`eth_getLogs`). The on-chain log data is cryptographically committed to the block through the Merkle Patricia Trie, so any tampering would change the block hash. However, the JSON-RPC layer is an untrusted intermediary: a compromised RPC endpoint (e.g. a malicious Infura fork or an MitM proxy) could return fabricated log data that LedgerLens would otherwise accept as authentic — causing false wash-trading alerts against innocent wallets.

LedgerLens implements two complementary defences:

### 1. Canonical event hash

For every ingested bridge event, `compute_canonical_event_hash` produces a deterministic SHA-256 fingerprint over the event's immutable fields:

```python
canonical = {
    "chain_id": int,
    "address": str,   # contract address, lowercase
    "topics": list,   # lowercase hex strings
    "data": str,      # ABI-encoded data, lowercase
    "block_number": int,
    "tx_hash": str,   # lowercase
    "log_index": int,
}
digest = SHA-256(json.dumps(canonical, sort_keys=True, separators=(",", ":")))
```

This hash is stored in the `bridge_transfers.canonical_hash` column. It reflects what LedgerLens ingested — not what a subsequent attacker might claim — and can be used for audit and replay detection.

### 2. Receipt-based log verification

`BridgeEventVerifier.verify_event_via_receipt` calls `eth_getTransactionReceipt` for the event's transaction hash and compares the log at `log_index` against the fields returned by `eth_getLogs`:

| Field compared | Rationale |
|---|---|
| `address` | Confirms the event came from the expected contract |
| `topics` | Confirms the event signature and indexed parameters |
| `data` | Confirms the non-indexed ABI-encoded payload |
| `blockHash` | Confirms the event's block provenance |

A mismatch on any field returns `VerificationResult.TAMPERED`.

#### Possible outcomes

| `VerificationResult` | Meaning |
|---|---|
| `verified` | Receipt matches — event is authentic |
| `tampered` | Receipt does not match — event rejected, DLQ routed |
| `receipt_not_found` | Transaction not yet mined or pruned from node |
| `log_index_out_of_range` | Receipt has fewer logs than expected |
| `skipped` | Event not selected by sampling (see below) |
| `disabled` | Verification turned off (`BRIDGE_VERIFY_SAMPLE_RATE=0`) |

#### Tampered event policy

Tampered events are:
- Logged at `ERROR` level with the transaction hash, log index, and chain ID (the full data field is intentionally excluded)
- Enqueued on the dead-letter queue with `error_class=SCHEMA_ERROR`
- **Not written** to the `bridge_transfers` table

### Sampling configuration

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_VERIFY_SAMPLE_RATE` | `1.0` | Fraction of events to verify (0.0 = disabled, 1.0 = all) |
| `BRIDGE_VERIFY_RECEIPT_TIMEOUT_SECONDS` | `10.0` | Per-call timeout for `eth_getTransactionReceipt` |

Setting `BRIDGE_VERIFY_SAMPLE_RATE=0.0` disables all verification and emits a `WARNING` on startup:

```
Bridge event verification disabled — cross-chain integrity not guaranteed.
```

**Security vs cost trade-off**: full verification (`1.0`) doubles the RPC call volume for bridge events. Statistical sampling (`0.1`–`0.5`) provides probabilistic assurance at reduced cost. For production deployments processing untrusted or third-party RPC endpoints, `1.0` is recommended.

### Limitation: not a full Merkle proof

Receipt verification relies on `eth_getTransactionReceipt` reaching a trustworthy node. It does **not** verify the Merkle proof against the block header (that would require `eth_getProof` and a trusted block hash source). For maximum security, configure `EVMProviderPool` (ISSUE-013) with multiple independent providers — a tampering attack would then need to compromise all providers simultaneously.
