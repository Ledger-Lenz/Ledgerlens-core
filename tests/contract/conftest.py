"""Pytest fixtures for contract tests."""

from typing import Iterator

import pytest

from tests.contract.helpers import find_free_port, start_server


@pytest.fixture(scope="module")
def live_provider_base_url() -> Iterator[str]:
    "Start the provider-state app and yield its base URL."
    from tests.contract.provider_states_app import app

    port = find_free_port()
    server, thread = start_server(app, port)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)