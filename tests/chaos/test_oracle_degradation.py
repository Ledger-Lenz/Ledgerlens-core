"""Chaos scenario: degraded connectivity to oracle nodes.

Each ``RemoteOracleNode`` below performs a network round-trip through the
``oracle_node_proxy`` Toxiproxy listener before signing, standing in for an
oracle signer reached over the network. ``OracleCoordinator`` is exercised
while that link is slow or down.

Documented degraded-mode behaviour (see ``chaos-mesh/README.md``):

* a slow oracle link (within the per-node timeout) still reaches quorum;
* an unreachable oracle link is treated as a failed signer — the coordinator
  reports an invalid quorum within a bounded time and ``submit_with_quorum``
  returns ``False`` without calling the publisher;
* a partial outage still reaches quorum from the healthy nodes.

Run with::

    docker compose --profile chaos up -d
    pytest tests/chaos/test_oracle_degradation.py -m chaos -v
"""

from __future__ import annotations

import os
import socket
import time

import pytest

pytestmark = pytest.mark.chaos

PROXY_NAME = "oracle_node_proxy"
ORACLE_LISTEN = "0.0.0.0:18003"
ORACLE_UPSTREAM = "redis:6379"
ORACLE_ADDR = ("localhost", 18003)
NODE_TIMEOUT_S = 2.0
THRESHOLD = 2
N_NODES = 3
SCORE_ARGS = ("G" + "A" * 55, "XLM/USDC", 80, True, True, int(time.time()), 90, 1)


class _Publisher:
    def __init__(self) -> None:
        self.calls = 0

    def submit_with_quorum(self, *args) -> bool:
        self.calls += 1
        return True


def _make_nodes(n: int, remote: int | None = None):
    """Build *n* nodes; the first *remote* go through the proxy, the rest are local."""
    from detection.oracle_node import OracleNode

    class RemoteOracleNode(OracleNode):
        def sign_score_submission(self, *args, **kwargs):
            with socket.create_connection(ORACLE_ADDR, timeout=NODE_TIMEOUT_S) as s:
                s.settimeout(NODE_TIMEOUT_S)
                s.sendall(b"PING\r\n")
                if not s.recv(16).startswith(b"+PONG"):
                    raise ConnectionError("oracle node did not acknowledge")
            return super().sign_score_submission(*args, **kwargs)

    nodes = []
    remote = n if remote is None else remote
    for i in range(n):
        var = f"CHAOS_ORACLE_KEY_{i}"
        os.environ[var] = os.urandom(32).hex()
        cls = RemoteOracleNode if i < remote else OracleNode
        nodes.append(cls(f"oracle-{i}", var))
    return nodes


@pytest.fixture(scope="module")
def oracle_proxy(toxiproxy):
    toxiproxy.create_proxy(PROXY_NAME, ORACLE_LISTEN, ORACLE_UPSTREAM)
    yield PROXY_NAME
    toxiproxy.reset_proxy(PROXY_NAME)
    toxiproxy.enable_proxy(PROXY_NAME)


@pytest.fixture(autouse=True)
def _clean_proxy(toxiproxy, oracle_proxy):
    toxiproxy.reset_proxy(oracle_proxy)
    toxiproxy.enable_proxy(oracle_proxy)
    yield
    toxiproxy.reset_proxy(oracle_proxy)
    toxiproxy.enable_proxy(oracle_proxy)


def test_slow_oracle_link_still_reaches_quorum(toxiproxy, oracle_proxy):
    from detection.oracle_coordinator import OracleCoordinator

    toxiproxy.add_latency(oracle_proxy, latency_ms=500, jitter_ms=100)
    coordinator = OracleCoordinator(_make_nodes(N_NODES), threshold=THRESHOLD)
    quorum = coordinator.collect_signatures(*SCORE_ARGS)
    assert quorum.is_valid_quorum
    assert quorum.signers_count == THRESHOLD


def test_unreachable_oracles_fail_quorum_without_submitting(toxiproxy, oracle_proxy):
    from detection.oracle_coordinator import OracleCoordinator

    toxiproxy.add_timeout(oracle_proxy, timeout_ms=0)
    coordinator = OracleCoordinator(_make_nodes(N_NODES), threshold=THRESHOLD)
    publisher = _Publisher()
    t0 = time.monotonic()
    assert coordinator.submit_with_quorum(*SCORE_ARGS, publisher=publisher) is False
    assert time.monotonic() - t0 < N_NODES * NODE_TIMEOUT_S + 2
    assert publisher.calls == 0


def test_partial_oracle_outage_reaches_quorum_from_healthy_nodes(toxiproxy, oracle_proxy):
    from detection.oracle_coordinator import OracleCoordinator

    toxiproxy.disable_proxy(oracle_proxy)
    # One node behind the dead link, two healthy local nodes.
    coordinator = OracleCoordinator(_make_nodes(N_NODES, remote=1), threshold=THRESHOLD)
    publisher = _Publisher()
    assert coordinator.submit_with_quorum(*SCORE_ARGS, publisher=publisher) is True
    assert publisher.calls == 1
