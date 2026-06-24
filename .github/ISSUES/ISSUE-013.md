---
title: "Implement Multi-Network EVM Provider Failover for Bridge Ingestion"
labels: ["difficulty: advanced", "area: ingestion", "type: reliability"]
assignees: []
---

## Summary
`ingestion/evm_loader.py` currently connects to a single EVM JSON-RPC endpoint per chain for bridge event ingestion, creating a single point of failure for the cross-chain detection feature. When the primary RPC provider experiences downtime or rate limiting, bridge event ingestion stops entirely, creating gaps in the cross-chain feature data. Implementing multi-provider failover with health probing and per-chain block-lag monitoring will make cross-chain ingestion resilient to individual provider failures.

## Background & Context
LedgerLens ingests bridge events from three EVM chains — Ethereum, Base, and Polygon — via `ingestion/evm_loader.py`. These events feed six cross-chain ML features (see README and `docs/cross_chain_detection.md`). Each chain requires a JSON-RPC provider to call `eth_getLogs`, `eth_getBlockNumber`, and `eth_getTransactionReceipt`.

Current production deployments typically use a single provider per chain (e.g., Infura, Alchemy, or a self-hosted node). These providers have:
- **Rate limits**: free tiers are aggressively rate-limited; spikes in LedgerLens queries can trigger 429s
- **Availability issues**: planned maintenance windows, regional outages, and DDoS attacks cause intermittent downtime
- **Block lag**: some providers (especially public endpoints) lag behind the canonical chain head by 1–10 blocks, causing missed recent events

The solution is an `EVMProviderPool` that maintains a list of providers per chain, continuously health-probes them by checking their reported `eth_blockNumber` relative to the known chain head, and automatically routes requests to the fastest/healthiest provider. When the primary provider fails a request, the pool transparently retries on the next healthy provider.

## Objectives
- [ ] Implement `EVMProviderPool` in `ingestion/evm_loader.py` that manages multiple JSON-RPC endpoints per chain with priority ordering, automatic failover, and health scoring.
- [ ] Implement a background `ProviderHealthProbe` coroutine that polls each provider's `eth_blockNumber` every configurable interval and updates health scores based on block lag and response latency.
- [ ] Add per-chain `block_lag_seconds` monitoring that computes the estimated time behind the chain head for each provider and raises an alert if all providers for a chain exceed a configurable lag threshold.
- [ ] Expose `EVMProviderPoolStats` (per-provider request count, error rate, current block lag, health score) accessible from the metrics layer.

## Technical Requirements

**`EVMProvider` dataclass:**
```python
@dataclass
class EVMProvider:
    chain_id: int
    rpc_url: str
    name: str                           # e.g. "infura-mainnet", "alchemy-mainnet"
    priority: int = 0                   # lower = higher priority
    max_requests_per_second: float = 10.0
    health_score: float = 1.0           # 0.0 (dead) to 1.0 (fully healthy)
    current_block: int = 0
    last_probe_at: datetime | None = None
    consecutive_failures: int = 0
    is_circuit_open: bool = False
```

**`EVMProviderPool` interface:**
```python
class EVMProviderPool:
    def __init__(
        self,
        providers: list[EVMProvider],
        max_block_lag: int = 10,          # blocks behind head before health degrades
        probe_interval_seconds: float = 15.0,
        circuit_breaker_threshold: int = 5,
    ): ...

    async def call(
        self,
        chain_id: int,
        method: str,
        params: list,
        timeout: float = 10.0,
    ) -> Any:
        """
        Execute a JSON-RPC call on the healthiest available provider for chain_id.
        Automatically fails over to the next provider on timeout or error.
        Raises EVMProviderPoolExhaustedError if all providers fail.
        """

    async def start_health_probing(self) -> None:
        """Start background task that probes all providers periodically."""

    async def stop_health_probing(self) -> None:
        """Cancel background probe task gracefully."""

    def get_best_provider(self, chain_id: int) -> EVMProvider | None:
        """Return the highest-health, lowest-priority provider for the chain."""

    @property
    def stats(self) -> EVMProviderPoolStats: ...
```

**Provider selection algorithm** — score each provider:
```python
def provider_score(p: EVMProvider, reference_block: int) -> float:
    if p.is_circuit_open:
        return -1.0
    lag_penalty = max(0, p.current_block - reference_block) * 0.1  # -0.1 per block lag
    return p.health_score - lag_penalty
```
Select the provider with the highest score. On tie, prefer lower `priority` number (higher-priority provider).

**Failover sequence:**
```python
async def call(self, chain_id, method, params, timeout=10.0):
    providers = self._sorted_providers(chain_id)
    last_error = None
    for provider in providers:
        if provider.is_circuit_open:
            continue
        try:
            result = await self._rpc_call(provider, method, params, timeout)
            provider.consecutive_failures = 0
            provider.health_score = min(1.0, provider.health_score + 0.05)
            return result
        except (asyncio.TimeoutError, EVMRPCError) as e:
            last_error = e
            provider.consecutive_failures += 1
            provider.health_score = max(0.0, provider.health_score - 0.2)
            if provider.consecutive_failures >= self.circuit_breaker_threshold:
                provider.is_circuit_open = True
                logger.error("Circuit opened for provider %s (chain %d)", provider.name, chain_id)
    raise EVMProviderPoolExhaustedError(chain_id, last_error)
```

**`ProviderHealthProbe` coroutine:**
```python
async def _probe_loop(self) -> None:
    while True:
        for provider in self._all_providers:
            try:
                block_hex = await self._rpc_call(provider, "eth_blockNumber", [], timeout=5.0)
                provider.current_block = int(block_hex, 16)
                provider.last_probe_at = datetime.utcnow()
                if provider.is_circuit_open and provider.consecutive_failures == 0:
                    provider.is_circuit_open = False   # reset circuit on successful probe
                    logger.info("Circuit reset for provider %s", provider.name)
            except Exception:
                pass  # probe failures handled by consecutive_failures tracking in call()
        await asyncio.sleep(self.probe_interval_seconds)
```

**Block lag alert**: compute `reference_block` as the max `current_block` across all providers for a chain. If all providers for a chain have `current_block < reference_block - max_block_lag`, emit a `WARNING` log and set a `lag_alert_active` flag on the pool (visible in `EVMProviderPoolStats`).

**`EVMProviderPoolStats`:**
```python
@dataclass
class EVMProviderStats:
    provider_name: str
    chain_id: int
    requests_total: int
    errors_total: int
    error_rate: float
    current_block: int
    block_lag: int              # blocks behind reference
    health_score: float
    is_circuit_open: bool

@dataclass
class EVMProviderPoolStats:
    providers: list[EVMProviderStats]
    chains_with_lag_alert: list[int]
```

**Configuration** — providers are configured via `config/settings.py` as a JSON list:
```
EVM_PROVIDERS=[
  {"chain_id": 1, "rpc_url": "https://mainnet.infura.io/v3/KEY", "name": "infura", "priority": 0},
  {"chain_id": 1, "rpc_url": "https://eth-mainnet.alchemyapi.io/v2/KEY", "name": "alchemy", "priority": 1}
]
```

Additional settings:
- `EVM_MAX_BLOCK_LAG`: default `10`
- `EVM_PROBE_INTERVAL_SECONDS`: default `15.0`
- `EVM_CIRCUIT_BREAKER_THRESHOLD`: default `5`

## Security Considerations
- RPC URLs must be validated to use `https://` scheme only — `http://` endpoints transmit API keys in plaintext and must be rejected at configuration load time with a `ValueError`.
- API keys embedded in RPC URLs (e.g., `infura.io/v3/SECRET`) must never appear in logs. The `EVMProvider.__repr__` must mask the URL after the third `/` to avoid key leakage: `"https://mainnet.infura.io/v3/***"`.
- The `EVMProviderPoolExhaustedError` message must not include the RPC URLs (which contain API keys) — only provider names and chain IDs.
- The circuit breaker prevents a misbehaving provider from causing infinite retry loops that could exhaust rate limits or incur API cost overruns.
- JSON-RPC `params` values from LedgerLens (block numbers, contract addresses) must be validated before serialisation to prevent injection of unexpected method calls via attacker-controlled parameter values.

## Testing Requirements
- Unit tests covering `provider_score()`: healthy provider, lagging provider, circuit-open provider (score < 0)
- Unit tests covering `EVMProviderPool.call()`: primary succeeds (no failover), primary fails (failover to secondary), all fail (raises `EVMProviderPoolExhaustedError`), circuit open on threshold consecutive failures
- Unit tests covering circuit reset: provider with open circuit succeeds on probe → circuit closed
- Unit tests covering block lag alert: all providers at least `max_block_lag` blocks behind → `lag_alert_active = True`
- Unit tests covering `ProviderHealthProbe`: mock `eth_blockNumber` returning incrementing blocks; assert `current_block` updated on each probe
- Integration tests: mock two providers for Ethereum; primary returns 429 → secondary serves the request; assert request count stats correct
- Edge cases: only one provider for a chain (no failover available), provider URL with no API key in path, chain with no configured providers, concurrent calls during circuit open/close transition
- Performance benchmark: 1,000 concurrent RPC calls routed through a 3-provider pool with one provider failing should complete in < 10 seconds

## Documentation Requirements
- Update `README.md` configuration table with `EVM_PROVIDERS` JSON format and `EVM_MAX_BLOCK_LAG`
- Add docstrings to `EVMProviderPool`, `ProviderHealthProbe`, `EVMProvider`, and `provider_score`
- Update `docs/cross_chain_detection.md` with a section on provider configuration, failover behaviour, and block lag monitoring
- Document how to mask API keys in `EVM_PROVIDERS` for log-safe configuration

## Definition of Done
- [ ] All objectives completed
- [ ] Tests pass (`pytest`)
- [ ] No regressions on existing test suite
- [ ] PR reviewed and approved

## For Contributors
**When applying for this issue, please specify:**
- Your area of specialty (e.g., Python backend, streaming systems, blockchain data, ML engineering)
- Relevant experience with: EVM JSON-RPC (`eth_getLogs`, `eth_blockNumber`), multi-provider failover patterns, circuit breaker pattern, Python asyncio
- Your approach or initial thoughts on the implementation
- Estimated time to complete

**Ideal contributor profile:** Engineer with experience operating EVM infrastructure at scale, particularly multi-provider setups with Infura, Alchemy, or self-hosted nodes. Deep understanding of JSON-RPC 2.0, circuit breaker patterns, and block lag monitoring. Python async proficiency is essential.
