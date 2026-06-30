"""EVM chain DEX trade ingestion via public JSON-RPC endpoints.

Fetches Uniswap V2/V3 Swap events from configured pool addresses and
parses them into CrossChainTrade records.  The token-bucket rate limiter
caps outbound RPC calls at 10 req/s per chain to avoid accidental DoS
against public endpoints.

Per-network circuit breakers isolate failures: a Base outage does not
affect Arbitrum ingestion and vice versa.

Multi-provider failover is implemented via :class:`EVMProviderPool`, which
manages a prioritised list of JSON-RPC endpoints per chain. A background
:meth:`EVMProviderPool.start_health_probing` coroutine continuously polls
``eth_blockNumber`` on each provider and updates health scores based on block
lag and response latency. On a request failure the pool automatically retries
on the next healthy provider; if all providers for a chain are exhausted it
raises :exc:`EVMProviderPoolExhaustedError`.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import aiohttp
import requests
from pydantic import BaseModel
from web3 import Web3

from config.settings import settings

logger = logging.getLogger("ledgerlens.evm_loader")

SUPPORTED_CHAINS = ["ethereum", "base", "polygon", "arbitrum"]

# ---------------------------------------------------------------------------
# Security helper
# ---------------------------------------------------------------------------

_URL_KEY_RE = re.compile(r"(https?://[^/]+/[^/]+/[^/]+/)([^?#]*)(.*)")


def _mask_rpc_url(url: str) -> str:
    """Return a log-safe representation of an RPC URL.

    Any path segment after the third ``/`` (where API keys live, e.g.
    ``infura.io/v3/SECRET``) is replaced with ``***``.  The scheme and host
    are preserved so log messages remain useful.

    Examples::

        >>> _mask_rpc_url("https://mainnet.infura.io/v3/abc123def")
        'https://mainnet.infura.io/v3/***'
        >>> _mask_rpc_url("https://eth.llamarpc.com")
        'https://eth.llamarpc.com'
    """
    m = _URL_KEY_RE.match(url)
    if m:
        return m.group(1) + "***" + (("?" + m.group(3).lstrip("?#")) if m.group(3).startswith("?") else "")
    return url


def _validate_rpc_url(url: str) -> str:
    """Raise ``ValueError`` if *url* is not an ``https://`` URL.

    HTTP endpoints transmit API keys in plaintext and are rejected at
    configuration load time rather than silently at runtime.
    """
    if not url.startswith("https://"):
        raise ValueError(
            f"EVM RPC URL must use https:// scheme to protect embedded API keys. "
            f"Got: {_mask_rpc_url(url)!r}"
        )
    return url


def _validate_rpc_params(params: list) -> list:
    """Validate JSON-RPC params to prevent injection of unexpected method calls.

    Accepts lists whose items are strings, ints, booleans, ``None``, or dicts
    whose values are also strings, ints, booleans, or ``None``.  Raises
    ``ValueError`` for any other structure.
    """
    _SCALAR = (str, int, bool, type(None))

    def _check(v: Any, depth: int = 0) -> None:
        if depth > 3:
            raise ValueError("RPC params nested too deeply (max depth 3)")
        if isinstance(v, _SCALAR):
            return
        if isinstance(v, list):
            for item in v:
                _check(item, depth + 1)
            return
        if isinstance(v, dict):
            for k, val in v.items():
                if not isinstance(k, str):
                    raise ValueError(f"RPC param dict key must be str, got {type(k)}")
                _check(val, depth + 1)
            return
        raise ValueError(f"Unexpected RPC param type {type(v)}: {v!r}")

    if not isinstance(params, list):
        raise ValueError(f"RPC params must be a list, got {type(params)}")
    for item in params:
        _check(item)
    return params


# ---------------------------------------------------------------------------
# EVMProvider dataclass
# ---------------------------------------------------------------------------


@dataclass
class EVMProvider:
    """Represents a single JSON-RPC endpoint for an EVM chain.

    Instances are managed by :class:`EVMProviderPool` and must not be shared
    across multiple pool instances without external synchronisation.

    Attributes:
        chain_id: EVM chain identifier (e.g. 1 = Ethereum mainnet).
        rpc_url: Full JSON-RPC endpoint URL.  Must use ``https://``.
        name: Human-readable name used in logs and stats (e.g. ``"infura"``).
            Must not contain the API key.
        priority: Lower values are tried first on tie (lower number = higher
            priority).  Default 0.
        max_requests_per_second: Token-bucket rate limit for this provider.
        health_score: Float in ``[0.0, 1.0]``.  1.0 = fully healthy,
            0.0 = dead.  Adjusted automatically by the pool.
        current_block: Most recently observed ``eth_blockNumber`` from the
            health probe.
        last_probe_at: UTC timestamp of the most recent successful probe.
        consecutive_failures: Number of consecutive call or probe failures.
        is_circuit_open: When ``True`` the pool skips this provider until a
            successful health probe resets it.
    """

    chain_id: int
    rpc_url: str
    name: str
    priority: int = 0
    max_requests_per_second: float = 10.0
    health_score: float = 1.0
    current_block: int = 0
    last_probe_at: datetime | None = None
    consecutive_failures: int = 0
    is_circuit_open: bool = False

    def __post_init__(self) -> None:
        _validate_rpc_url(self.rpc_url)

    def __repr__(self) -> str:
        # API keys embedded in the URL must never appear in logs or repr.
        return (
            f"EVMProvider(chain_id={self.chain_id}, name={self.name!r}, "
            f"rpc_url={_mask_rpc_url(self.rpc_url)!r}, "
            f"priority={self.priority}, health_score={self.health_score:.2f}, "
            f"is_circuit_open={self.is_circuit_open})"
        )


# ---------------------------------------------------------------------------
# Stats dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EVMProviderStats:
    """Per-provider statistics snapshot.

    Attributes:
        provider_name: Matches :attr:`EVMProvider.name`.
        chain_id: EVM chain identifier.
        requests_total: Total RPC calls attempted through this provider.
        errors_total: Total failed RPC calls (including circuit-open skips).
        error_rate: ``errors_total / requests_total`` or ``0.0`` when no
            requests have been made.
        current_block: Most recently probed block number (0 if never probed).
        block_lag: Blocks behind the reference (chain head) at snapshot time.
        health_score: Current health score in ``[0.0, 1.0]``.
        is_circuit_open: Whether the circuit breaker is currently open.
    """

    provider_name: str
    chain_id: int
    requests_total: int
    errors_total: int
    error_rate: float
    current_block: int
    block_lag: int
    health_score: float
    is_circuit_open: bool


@dataclass
class EVMProviderPoolStats:
    """Aggregate statistics for the entire provider pool.

    Attributes:
        providers: One :class:`EVMProviderStats` entry per configured provider.
        chains_with_lag_alert: Chain IDs where all providers are lagging
            beyond :attr:`EVMProviderPool.max_block_lag` blocks behind the
            reference block.
    """

    providers: list[EVMProviderStats]
    chains_with_lag_alert: list[int]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EVMRPCError(Exception):
    """Raised when a JSON-RPC call returns an ``error`` field in its response.

    The error code and message from the server are preserved but the RPC URL
    (which may contain an API key) is never included in the message.
    """

    def __init__(self, provider_name: str, chain_id: int, method: str, error: dict) -> None:
        self.provider_name = provider_name
        self.chain_id = chain_id
        self.method = method
        self.error = error
        super().__init__(
            f"JSON-RPC error from provider {provider_name!r} (chain {chain_id}) "
            f"calling {method!r}: code={error.get('code')}, "
            f"message={error.get('message')!r}"
        )


class EVMProviderPoolExhaustedError(Exception):
    """Raised by :meth:`EVMProviderPool.call` when every provider for a chain
    has been tried and all failed (or had open circuits).

    The RPC URLs are deliberately excluded from the message to prevent API key
    leakage in logs.  Only provider names and the chain ID are included.
    """

    def __init__(self, chain_id: int, last_error: Exception | None, provider_names: list[str]) -> None:
        self.chain_id = chain_id
        self.last_error = last_error
        self.provider_names = provider_names
        super().__init__(
            f"All providers exhausted for chain {chain_id}. "
            f"Tried: {provider_names}. "
            f"Last error: {type(last_error).__name__}: {last_error}"
        )


# ---------------------------------------------------------------------------
# EVMProviderPool
# ---------------------------------------------------------------------------


class EVMProviderPool:
    """Manages multiple JSON-RPC endpoints per EVM chain with automatic
    failover, health scoring, and block-lag monitoring.

    Usage::

        pool = EVMProviderPool(providers=[...])
        await pool.start_health_probing()
        result = await pool.call(chain_id=1, method="eth_blockNumber", params=[])
        await pool.stop_health_probing()

    Provider selection
    ------------------
    On each :meth:`call`, providers for the requested chain are ranked by
    :func:`provider_score`.  The highest-scoring non-circuit-open provider is
    tried first.  On failure the pool moves to the next provider.  If all
    providers fail, :exc:`EVMProviderPoolExhaustedError` is raised.

    Health probing
    --------------
    A background asyncio task (started via :meth:`start_health_probing`) polls
    every provider's ``eth_blockNumber`` every
    :attr:`probe_interval_seconds` seconds.  Successful probes update
    :attr:`EVMProvider.current_block` and :attr:`EVMProvider.last_probe_at`.
    A provider whose circuit was opened and then succeeds on a probe has its
    circuit reset automatically.

    Block-lag alerts
    ----------------
    After each probe cycle, if *all* providers for a chain have
    ``current_block < reference_block - max_block_lag`` (where
    ``reference_block`` is the maximum ``current_block`` across all providers),
    a WARNING is logged and the chain ID is added to
    :attr:`EVMProviderPoolStats.chains_with_lag_alert`.

    Security
    --------
    - RPC URLs are validated at construction time to require ``https://``.
    - API keys embedded in URLs never appear in log messages or exceptions.
    - JSON-RPC params are validated before serialisation.
    - Circuit breakers prevent retry storms that could exhaust rate limits.

    Args:
        providers: List of :class:`EVMProvider` instances to manage.
        max_block_lag: Number of blocks behind the reference block before a
            provider's health score is penalised (also triggers lag alerts
            when *all* providers exceed this threshold).
        probe_interval_seconds: Seconds between health probe cycles.
        circuit_breaker_threshold: Consecutive failures before a provider's
            circuit is opened.
    """

    def __init__(
        self,
        providers: list[EVMProvider],
        max_block_lag: int = 10,
        probe_interval_seconds: float = 15.0,
        circuit_breaker_threshold: int = 5,
    ) -> None:
        self._all_providers: list[EVMProvider] = list(providers)
        self.max_block_lag = max_block_lag
        self.probe_interval_seconds = probe_interval_seconds
        self.circuit_breaker_threshold = circuit_breaker_threshold

        # Per-provider counters (keyed by provider name + chain_id tuple)
        self._requests_total: dict[tuple[str, int], int] = {}
        self._errors_total: dict[tuple[str, int], int] = {}
        for p in self._all_providers:
            key = (p.name, p.chain_id)
            self._requests_total.setdefault(key, 0)
            self._errors_total.setdefault(key, 0)

        # Lag alert state per chain
        self._lag_alert_chains: set[int] = set()

        # Background probe task
        self._probe_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def call(
        self,
        chain_id: int,
        method: str,
        params: list,
        timeout: float = 10.0,
    ) -> Any:
        """Execute a JSON-RPC call on the healthiest available provider.

        Iterates over providers for *chain_id* in score order, attempting each
        one.  On ``asyncio.TimeoutError`` or :exc:`EVMRPCError` the provider's
        health score is decremented; after
        :attr:`circuit_breaker_threshold` consecutive failures the circuit is
        opened and the provider is skipped.  Successful calls increment the
        health score.

        Args:
            chain_id: Target EVM chain.
            method: JSON-RPC method name (e.g. ``"eth_getLogs"``).
            params: JSON-RPC params list — validated for injection safety.
            timeout: Per-call timeout in seconds.

        Returns:
            The ``result`` value from the JSON-RPC response.

        Raises:
            EVMProviderPoolExhaustedError: When every provider for *chain_id*
                has failed or has an open circuit.
        """
        _validate_rpc_params(params)
        providers = self._sorted_providers(chain_id)
        if not providers:
            raise EVMProviderPoolExhaustedError(chain_id, None, [])

        last_error: Exception | None = None
        tried: list[str] = []

        for provider in providers:
            if provider.is_circuit_open:
                continue
            key = (provider.name, provider.chain_id)
            self._requests_total[key] = self._requests_total.get(key, 0) + 1
            tried.append(provider.name)
            try:
                result = await self._rpc_call(provider, method, params, timeout)
                provider.consecutive_failures = 0
                provider.health_score = min(1.0, provider.health_score + 0.05)
                return result
            except (asyncio.TimeoutError, EVMRPCError, aiohttp.ClientError) as exc:
                last_error = exc
                self._errors_total[key] = self._errors_total.get(key, 0) + 1
                provider.consecutive_failures += 1
                provider.health_score = max(0.0, provider.health_score - 0.2)
                if provider.consecutive_failures >= self.circuit_breaker_threshold:
                    provider.is_circuit_open = True
                    logger.error(
                        "Circuit opened for provider %r (chain %d) after %d consecutive failures",
                        provider.name,
                        chain_id,
                        provider.consecutive_failures,
                    )

        raise EVMProviderPoolExhaustedError(chain_id, last_error, tried)

    async def start_health_probing(self) -> None:
        """Start the background health probe coroutine.

        The probe runs every :attr:`probe_interval_seconds` and updates each
        provider's :attr:`~EVMProvider.current_block`,
        :attr:`~EVMProvider.last_probe_at`, and circuit state.

        Safe to call multiple times — calling when a probe is already running
        is a no-op.
        """
        if self._probe_task is not None and not self._probe_task.done():
            return
        self._probe_task = asyncio.get_event_loop().create_task(
            self._probe_loop(), name="evm_provider_health_probe"
        )

    async def stop_health_probing(self) -> None:
        """Cancel the background health probe task and wait for it to finish.

        Safe to call when no probe is running.
        """
        if self._probe_task is None or self._probe_task.done():
            return
        self._probe_task.cancel()
        try:
            await self._probe_task
        except asyncio.CancelledError:
            pass
        self._probe_task = None

    def get_best_provider(self, chain_id: int) -> EVMProvider | None:
        """Return the highest-scoring, non-circuit-open provider for *chain_id*.

        Returns ``None`` if no providers are configured for the chain or all
        have open circuits.
        """
        candidates = self._sorted_providers(chain_id)
        for p in candidates:
            if not p.is_circuit_open:
                return p
        return None

    @property
    def stats(self) -> EVMProviderPoolStats:
        """Return a snapshot of per-provider statistics and lag alerts.

        The block lag for each provider is computed relative to the maximum
        ``current_block`` across all providers on the same chain (the reference
        block).
        """
        # Compute reference block per chain
        reference_blocks: dict[int, int] = {}
        for p in self._all_providers:
            if p.current_block > reference_blocks.get(p.chain_id, 0):
                reference_blocks[p.chain_id] = p.current_block

        provider_stats: list[EVMProviderStats] = []
        for p in self._all_providers:
            key = (p.name, p.chain_id)
            req = self._requests_total.get(key, 0)
            err = self._errors_total.get(key, 0)
            ref = reference_blocks.get(p.chain_id, 0)
            lag = max(0, ref - p.current_block)
            provider_stats.append(
                EVMProviderStats(
                    provider_name=p.name,
                    chain_id=p.chain_id,
                    requests_total=req,
                    errors_total=err,
                    error_rate=err / req if req > 0 else 0.0,
                    current_block=p.current_block,
                    block_lag=lag,
                    health_score=p.health_score,
                    is_circuit_open=p.is_circuit_open,
                )
            )

        return EVMProviderPoolStats(
            providers=provider_stats,
            chains_with_lag_alert=sorted(self._lag_alert_chains),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sorted_providers(self, chain_id: int) -> list[EVMProvider]:
        """Return providers for *chain_id* sorted by descending provider score.

        On score tie, lower :attr:`~EVMProvider.priority` (higher priority)
        wins.  Providers with open circuits are included (scored at -1) so the
        caller can decide whether to skip them.
        """
        chain_providers = [p for p in self._all_providers if p.chain_id == chain_id]
        reference_block = max((p.current_block for p in chain_providers), default=0)
        return sorted(
            chain_providers,
            key=lambda p: (-provider_score(p, reference_block), p.priority),
        )

    async def _rpc_call(
        self,
        provider: EVMProvider,
        method: str,
        params: list,
        timeout: float,
    ) -> Any:
        """Issue a single JSON-RPC 2.0 call to *provider* asynchronously.

        Returns the ``result`` value on success.  Raises :exc:`EVMRPCError`
        when the response contains an ``error`` field, or
        ``asyncio.TimeoutError`` / :exc:`aiohttp.ClientError` on transport
        failures.  The provider's RPC URL is never included in raised
        exceptions.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.post(
                    provider.rpc_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    response.raise_for_status()
                    data = await response.json(content_type=None)
            except asyncio.TimeoutError:
                raise
            except aiohttp.ClientResponseError as exc:
                # Wrap HTTP-level errors without leaking the URL
                raise aiohttp.ClientError(
                    f"HTTP {exc.status} from provider {provider.name!r} "
                    f"(chain {provider.chain_id}) calling {method!r}"
                ) from exc

        if "error" in data:
            raise EVMRPCError(provider.name, provider.chain_id, method, data["error"])
        return data["result"]

    async def _probe_loop(self) -> None:
        """Background coroutine that probes every provider periodically.

        On each cycle:

        1. Calls ``eth_blockNumber`` on every provider.
        2. Updates :attr:`~EVMProvider.current_block` and
           :attr:`~EVMProvider.last_probe_at` on success.
        3. Resets the circuit if a previously open provider responds
           successfully and its :attr:`~EVMProvider.consecutive_failures`
           counter has been cleared by :meth:`call`.
        4. Checks block-lag alerts across all chains.

        Probe failures do not directly increment
        :attr:`~EVMProvider.consecutive_failures` — that counter is only
        incremented by :meth:`call` because probes use a shorter timeout and
        intermittent probe errors should not penalise a healthy provider.
        """
        while True:
            for provider in self._all_providers:
                try:
                    block_hex = await self._rpc_call(
                        provider, "eth_blockNumber", [], timeout=5.0
                    )
                    provider.current_block = int(block_hex, 16)
                    provider.last_probe_at = datetime.now(tz=timezone.utc)
                    if provider.is_circuit_open and provider.consecutive_failures == 0:
                        provider.is_circuit_open = False
                        logger.info(
                            "Circuit reset for provider %r (chain %d) after successful probe",
                            provider.name,
                            provider.chain_id,
                        )
                except Exception:
                    # Probe failures are informational; consecutive_failures
                    # is only mutated by call() so we don't double-penalise.
                    logger.debug(
                        "Health probe failed for provider %r (chain %d)",
                        provider.name,
                        provider.chain_id,
                    )

            self._check_lag_alerts()
            await asyncio.sleep(self.probe_interval_seconds)

    def _check_lag_alerts(self) -> None:
        """Emit WARNING and update lag alert state when all providers on a
        chain are behind by more than :attr:`max_block_lag` blocks.
        """
        # Group providers by chain
        chains: dict[int, list[EVMProvider]] = {}
        for p in self._all_providers:
            chains.setdefault(p.chain_id, []).append(p)

        for chain_id, chain_providers in chains.items():
            if not chain_providers:
                continue
            reference_block = max(p.current_block for p in chain_providers)
            if reference_block == 0:
                # No probe has succeeded yet; skip alert evaluation
                continue
            all_lagging = all(
                p.current_block < reference_block - self.max_block_lag
                for p in chain_providers
            )
            if all_lagging:
                if chain_id not in self._lag_alert_chains:
                    logger.warning(
                        "All providers for chain %d are lagging > %d blocks behind "
                        "reference block %d. Cross-chain ingestion may be incomplete.",
                        chain_id,
                        self.max_block_lag,
                        reference_block,
                    )
                self._lag_alert_chains.add(chain_id)
            else:
                self._lag_alert_chains.discard(chain_id)


# ---------------------------------------------------------------------------
# Provider scoring
# ---------------------------------------------------------------------------


def provider_score(p: EVMProvider, reference_block: int) -> float:
    """Compute a numeric score for *p* relative to the chain's *reference_block*.

    The score determines provider preference inside
    :meth:`EVMProviderPool._sorted_providers`.  Higher scores are preferred.

    Algorithm:

    - Circuit-open providers always score ``-1.0`` so they are sorted to the
      end and skipped.
    - For healthy providers: ``health_score - lag_penalty``, where
      ``lag_penalty = max(0, reference_block - p.current_block) * 0.1``.
      Each block behind the reference costs ``0.1`` health points.

    Args:
        p: The provider to score.
        reference_block: The highest ``current_block`` seen across all
            providers on the same chain (the estimated canonical chain head).

    Returns:
        A float score.  Range is approximately ``(-inf, 1.0]``; circuit-open
        providers always return ``-1.0``.
    """
    if p.is_circuit_open:
        return -1.0
    lag_penalty = max(0, reference_block - p.current_block) * 0.1
    return p.health_score - lag_penalty

# Per-network default rate limits (req/s)
_NETWORK_RATE_LIMITS: dict[str, float] = {
    "ethereum": 10.0,
    "base": 10.0,
    "polygon": 10.0,
    "arbitrum": 10.0,
}

# Uniswap V3: Swap(address indexed sender, address indexed recipient,
#                  int256 amount0, int256 amount1, uint160 sqrtPriceX96,
#                  uint128 liquidity, int24 tick)
UNISWAP_V3_SWAP_TOPIC = "0x" + Web3.keccak(
    text="Swap(address,address,int256,int256,uint160,uint128,int24)"
).hex()

# Uniswap V2: Swap(address indexed sender, uint256 amount0In, uint256 amount1In,
#                  uint256 amount0Out, uint256 amount1Out, address indexed to)
UNISWAP_V2_SWAP_TOPIC = "0x" + Web3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).hex()


class CrossChainTrade(BaseModel):
    chain: str
    tx_hash: str
    block_number: int
    block_timestamp: datetime
    pool_address: str
    wallet_address: str
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float


class _TokenBucket:
    """Thread-safe token bucket for rate limiting (tokens/second)."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            sleep_time = (1.0 - self._tokens) / self._rate
            time.sleep(sleep_time)
            self._tokens = 0.0


class _CircuitBreaker:
    """Per-network circuit breaker.

    After `threshold` consecutive failures the circuit opens and all calls
    raise ``CircuitOpenError``.  The circuit auto-resets after `reset_seconds`.
    """

    class CircuitOpenError(Exception):
        pass

    def __init__(self, threshold: int = 5, reset_seconds: float = 300.0) -> None:
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self._reset_seconds:
                # Auto-reset
                self._failures = 0
                self._opened_at = None
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = time.monotonic()

    def check(self) -> None:
        """Raise CircuitOpenError if the circuit is open."""
        if self.is_open:
            raise _CircuitBreaker.CircuitOpenError(
                f"Circuit open after {self._failures} consecutive failures; "
                f"resets in {self._reset_seconds}s"
            )


def _validate_evm_address(address: str) -> str:
    """Return the EIP-55 checksummed form of address, or raise ValueError."""
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        raise ValueError(f"Malformed EVM address (must be 42-char hex starting with 0x): {address!r}")
    try:
        return Web3.to_checksum_address(address)
    except Exception as exc:
        raise ValueError(f"Invalid EVM address {address!r}: {exc}") from exc


def _decode_address_from_topic(topic: str) -> str:
    """Extract and checksum an EVM address from a 32-byte topic field."""
    if topic.startswith("0x"):
        topic = topic[2:]
    raw = "0x" + topic[-40:]
    return Web3.to_checksum_address(raw)


class EVMTradeLoader:
    """Fetch Uniswap V2/V3 Swap events from EVM chains via JSON-RPC."""

    def __init__(
        self,
        chain: str,
        rpc_url: str,
        pool_addresses: list[str] | None = None,
        _rate_limiter: _TokenBucket | None = None,
        _circuit_breaker: _CircuitBreaker | None = None,
    ) -> None:
        if chain not in SUPPORTED_CHAINS:
            raise ValueError(
                f"Unsupported chain: {chain!r}. Supported chains: {SUPPORTED_CHAINS}"
            )
        self.chain = chain
        self._rpc_url = rpc_url
        self.pool_addresses = [_validate_evm_address(a) for a in (pool_addresses or [])]
        rate = _NETWORK_RATE_LIMITS.get(chain, 10.0)
        self._rate_limiter = _rate_limiter or _TokenBucket(rate=rate)
        self._circuit_breaker = _circuit_breaker or _CircuitBreaker()

    def _rpc_call(self, method: str, params: list, max_retries: int = 3) -> dict:
        """Issue a JSON-RPC call with exponential backoff on 429 / transport errors."""
        self._circuit_breaker.check()
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            self._rate_limiter.acquire()
            logger.debug("RPC %s attempt %d/%d", method, attempt + 1, max_retries + 1)
            try:
                response = requests.post(self._rpc_url, json=payload, timeout=30)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self._circuit_breaker.record_failure()
                if attempt < max_retries:
                    time.sleep(2**attempt)
                continue

            if response.status_code == 429:
                wait = 2**attempt
                logger.debug("Rate-limited by RPC endpoint; retrying in %ss", wait)
                time.sleep(wait)
                last_exc = requests.HTTPError(
                    "429 Too Many Requests from RPC endpoint", response=response
                )
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                last_exc = exc
                self._circuit_breaker.record_failure()
                if attempt < max_retries:
                    time.sleep(2**attempt)
                continue

            result = response.json()
            if "error" in result:
                self._circuit_breaker.record_failure()
                raise ValueError(f"JSON-RPC error from {method}: {result['error']}")
            self._circuit_breaker.record_success()
            return result

        assert last_exc is not None
        raise last_exc

    def _get_latest_block(self) -> int:
        return int(self._rpc_call("eth_blockNumber", [])["result"], 16)

    def _get_block_timestamp(self, block_number: int) -> datetime:
        result = self._rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        ts = int(result["result"]["timestamp"], 16)
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _get_logs(
        self, from_block: int, to_block: int, address: str, topics: list[str]
    ) -> list[dict]:
        params = [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": address,
                "topics": [topics[0]] if topics else [],
            }
        ]
        return self._rpc_call("eth_getLogs", params)["result"]

    def _parse_v3_swap(
        self, log: dict, pool_address: str, block_timestamp: datetime
    ) -> CrossChainTrade:
        """Parse a Uniswap V3 Swap event log into a CrossChainTrade.

        Raises ValueError (not KeyError) when required fields are missing or malformed.
        """
        try:
            topics = log["topics"]
            sender = _decode_address_from_topic(topics[1])
            data_hex = log["data"]
            if data_hex.startswith("0x"):
                data_hex = data_hex[2:]
            if len(data_hex) < 128:
                raise ValueError(
                    f"Swap event data too short ({len(data_hex)} hex chars, expected ≥128): {data_hex!r}"
                )
            # amount0 and amount1 are signed int256 (first two 32-byte words)
            amount0 = int.from_bytes(bytes.fromhex(data_hex[0:64]), "big", signed=True)
            amount1 = int.from_bytes(bytes.fromhex(data_hex[64:128]), "big", signed=True)
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"Failed to parse V3 Swap event — missing field {exc}: {log!r}"
            ) from exc

        # In V3, amounts are from the pool's perspective:
        # amount0 < 0 means pool sends token0 to user (user receives token0, pays token1)
        # amount0 > 0 means pool receives token0 from user (user pays token0, receives token1)
        if amount0 < 0:
            token_in, token_out = "token1", "token0"
            amount_in = abs(amount1) / 1e18
            amount_out = abs(amount0) / 1e18
        else:
            token_in, token_out = "token0", "token1"
            amount_in = abs(amount0) / 1e18
            amount_out = abs(amount1) / 1e18

        return CrossChainTrade(
            chain=self.chain,
            tx_hash=log["transactionHash"],
            block_number=int(log["blockNumber"], 16),
            block_timestamp=block_timestamp,
            pool_address=Web3.to_checksum_address(pool_address),
            wallet_address=sender,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=amount_out,
        )

    def _parse_v2_swap(
        self, log: dict, pool_address: str, block_timestamp: datetime
    ) -> CrossChainTrade:
        """Parse a Uniswap V2 Swap event log into a CrossChainTrade.

        Raises ValueError (not KeyError) when required fields are missing or malformed.
        """
        try:
            topics = log["topics"]
            sender = _decode_address_from_topic(topics[1])
            data_hex = log["data"]
            if data_hex.startswith("0x"):
                data_hex = data_hex[2:]
            if len(data_hex) < 256:
                raise ValueError(
                    f"V2 Swap event data too short ({len(data_hex)} chars): {data_hex!r}"
                )
            # V2 data: amount0In, amount1In, amount0Out, amount1Out (each uint256 = 32 bytes)
            amount0_in = int.from_bytes(bytes.fromhex(data_hex[0:64]), "big")
            amount1_in = int.from_bytes(bytes.fromhex(data_hex[64:128]), "big")
            amount0_out = int.from_bytes(bytes.fromhex(data_hex[128:192]), "big")
            amount1_out = int.from_bytes(bytes.fromhex(data_hex[192:256]), "big")
        except (KeyError, IndexError) as exc:
            raise ValueError(
                f"Failed to parse V2 Swap event — missing field {exc}: {log!r}"
            ) from exc

        if amount0_in > 0:
            amount_in = amount0_in / 1e18
            amount_out = amount1_out / 1e18
            token_in, token_out = "token0", "token1"
        else:
            amount_in = amount1_in / 1e18
            amount_out = amount0_out / 1e18
            token_in, token_out = "token1", "token0"

        return CrossChainTrade(
            chain=self.chain,
            tx_hash=log["transactionHash"],
            block_number=int(log["blockNumber"], 16),
            block_timestamp=block_timestamp,
            pool_address=Web3.to_checksum_address(pool_address),
            wallet_address=sender,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=amount_out,
        )

    def load_trades(self, lookback_blocks: int | None = None) -> list[CrossChainTrade]:
        """Fetch Swap events from all configured pool addresses.

        Paginates over the last `lookback_blocks` blocks (default from settings).
        ABI mismatches between networks are handled gracefully: a warning is
        logged, the event is skipped, and ingestion continues.
        """
        lookback_blocks = lookback_blocks if lookback_blocks is not None else settings.evm_lookback_blocks
        latest = self._get_latest_block()
        from_block = max(0, latest - lookback_blocks)

        trades: list[CrossChainTrade] = []
        for pool_address in self.pool_addresses:
            # Try V3 first, fall back to V2
            for topic, parser in [
                (UNISWAP_V3_SWAP_TOPIC, self._parse_v3_swap),
                (UNISWAP_V2_SWAP_TOPIC, self._parse_v2_swap),
            ]:
                try:
                    logs = self._get_logs(from_block, latest, pool_address, [topic])
                    block_ts_cache: dict[int, datetime] = {}
                    for log in logs:
                        block_num = int(log["blockNumber"], 16)
                        if block_num not in block_ts_cache:
                            block_ts_cache[block_num] = self._get_block_timestamp(block_num)
                        try:
                            trade = parser(log, pool_address, block_ts_cache[block_num])
                            trades.append(trade)
                        except ValueError as exc:
                            # ABI mismatch or malformed event — log and skip, do not crash
                            logger.warning(
                                "ABI mismatch or malformed event on %s pool %s tx %s: %s",
                                self.chain,
                                pool_address,
                                log.get("transactionHash", "unknown"),
                                exc,
                            )
                            continue
                    if logs:
                        break
                except ValueError:
                    raise
                except _CircuitBreaker.CircuitOpenError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch %s logs for pool %s on %s: %s",
                        topic[:10],
                        pool_address,
                        self.chain,
                        exc,
                    )

        return trades
