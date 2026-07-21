
import os
import time
import subprocess
from pathlib import Path
from typing import Generator

import pytest
import requests
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from config.settings import settings

# New marker for cross-repo E2E tests
pytestmark = pytest.mark.cross_repo_e2e


def _skip_if_docker_unavailable():
    """Skip the test suite if Docker is not available."""
    try:
        subprocess.run(["docker", "--version"], check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Docker not available; skipping cross-repo E2E tests.")


def _resolve_repo_path(env_var: str, repo_name: str, pinned_ref: str) -> Path:
    """Return a local checkout path from env_var if set, otherwise git-clone
    Ledger-Lenz/{repo_name} at pinned_ref into a session tempdir.
    """
    from tempfile import mkdtemp

    # Check if environment variable is set
    env_path = os.environ.get(env_var)
    if env_path:
        path = Path(env_path).resolve()
        if not path.exists():
            pytest.skip(f"{env_var} set to {env_path}, but path does not exist.")
        return path

    # Check if git is available
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(
            f"Neither {env_var} set nor git available; "
            f"cannot check out {repo_name}."
        )

    # Clone the repo
    temp_dir = Path(mkdtemp())
    repo_url = f"https://github.com/Ledger-Lenz/{repo_name}.git"
    try:
        subprocess.run(
            ["git", "clone", repo_url, str(temp_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "checkout", pinned_ref],
            cwd=str(temp_dir),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        pytest.skip(
            f"Failed to clone {repo_name} at {pinned_ref}: {e.stderr}"
        )
    return temp_dir


@pytest.fixture(scope="session")
def api_repo_path() -> Path:
    """Resolve path to ledgerlens-api repo."""
    pinned_ref = os.environ.get("CROSS_REPO_E2E_PINNED_REF", "main")
    return _resolve_repo_path("LEDGERLENS_API_REPO_PATH", "ledgerlens-api", pinned_ref)


@pytest.fixture(scope="session")
def contracts_repo_path() -> Path:
    """Resolve path to ledgerlens-contracts repo."""
    pinned_ref = os.environ.get("CROSS_REPO_E2E_PINNED_REF", "main")
    return _resolve_repo_path("LEDGERLENS_CONTRACTS_REPO_PATH", "ledgerlens-contracts", pinned_ref)


@pytest.fixture(scope="session")
def soroban_testnet_container() -> Generator[DockerContainer, None, None]:
    """Start a local Soroban testnet container."""
    _skip_if_docker_unavailable()

    # Fail hard if NETWORK_PASSPHRASE is production
    production_passphrases = [
        "Public Global Stellar Network ; September 2015",
    ]
    if settings.soroban_network_passphrase in production_passphrases:
        pytest.fail("Cowardly refusing to run cross-repo E2E tests against production Soroban!")

    container = (
        DockerContainer("stellar/quickstart:testing")
        .with_command("--testnet --enable-soroban-rpc")
        .with_exposed_ports(8000)
    )

    with container:
        wait_for_logs(container, "soroban-rpc: server started", timeout=120)
        yield container


def _wait_for_soroban_rpc_ready(container: DockerContainer) -> None:
    """Wait until Soroban RPC is ready."""
    rpc_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8000)}/rpc"
    timeout = 120
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getHealth",
                    "params": {},
                },
                timeout=5,
            )
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    pytest.fail("Soroban RPC did not become ready in time.")


@pytest.fixture(scope="session")
def deployed_score_contract(soroban_testnet_container, contracts_repo_path) -> str:
    """Build and deploy ledgerlens-score contract, return deployed contract ID."""
    # TODO: Implement contract deployment using soroban-cli inside the container or a helper container
    pytest.skip("Contract deployment not yet implemented.")
    return "C..."


@pytest.fixture(scope="session")
def ledgerlens_api_container(deployed_score_contract, api_repo_path) -> Generator[DockerContainer, None, None]:
    """Build and start ledgerlens-api container pointing at deployed contract."""
    container = (
        DockerContainer.from_dockerfile(str(api_repo_path))
        .with_env("LEDGERLENS_SCORE_CONTRACT_ID", deployed_score_contract)
        .with_env("SOROBAN_RPC_URL", f"http://{soroban_testnet_container.get_container_host_ip()}:{soroban_testnet_container.get_exposed_port(8000)}/rpc")
        .with_env("SOROBAN_NETWORK_PASSPHRASE", "Test SDF Network ; September 2015")
        .with_exposed_ports(8000)
    )

    with container:
        yield container


@pytest.fixture(scope="session")
def api_base_url(ledgerlens_api_container) -> str:
    """Base URL for ledgerlens-api."""
    return f"http://{ledgerlens_api_container.get_container_host_ip()}:{ledgerlens_api_container.get_exposed_port(8000)}"

