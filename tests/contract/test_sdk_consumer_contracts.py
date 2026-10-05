"""Provider verification of the SDK consumer contracts (Issue #1039).

Each SDK (TypeScript, Go, Rust) publishes the requests it actually makes and
the response shape it depends on as a pact file under
``tests/contract/pacts/sdk/``. This module replays every interaction against
the real ``api.main`` application (isolated SQLite DB, seeded per provider
state) and fails when the API no longer satisfies a published contract.

Redirects are followed, mirroring the SDK HTTP clients, so SDKs that still
call the legacy bare paths are verified through the ``/v1`` redirect.

See ``docs/contract_testing.md`` ("SDK consumer contracts") for how to add
new expectations.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

PACT_DIR = Path(__file__).parent / "pacts" / "sdk"
PACT_FILES = sorted(PACT_DIR.glob("*.json"))

_SEEDED_STATE = re.compile(r"^a risk score exists for wallet (G[A-Z2-7]{55})$")
_EMPTY_STATE = re.compile(r"^no risk score exists for wallet (G[A-Z2-7]{55})$")


def shape_mismatches(expected, actual, path: str = "$") -> list[str]:
    """Return every place ``actual`` does not match ``expected`` by type."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, got {type(actual).__name__}"]
        errors: list[str] = []
        for key, value in expected.items():
            if key not in actual:
                errors.append(f"{path}.{key}: missing")
            else:
                errors.extend(shape_mismatches(value, actual[key], f"{path}.{key}"))
        return errors
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected array, got {type(actual).__name__}"]
        if expected and not actual:
            return [f"{path}: expected at least one element"]
        return shape_mismatches(expected[0], actual[0], f"{path}[0]") if expected else []
    if isinstance(expected, bool) or isinstance(actual, bool):
        ok = isinstance(expected, bool) and isinstance(actual, bool)
    elif isinstance(expected, (int, float)):
        ok = isinstance(actual, (int, float))
    else:
        ok = expected is None or type(actual) is type(expected)
    return (
        [] if ok else [f"{path}: expected {type(expected).__name__}, got {type(actual).__name__}"]
    )


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import config.settings as settings_module
    from api.main import app
    from detection.storage import init_db
    from detection.wallet_override_store import init_override_table

    db_path = str(tmp_path / "ledgerlens.db")
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    object.__setattr__(settings_module.settings, "ledgerlens_db_path", db_path)
    init_db()
    init_override_table()
    return TestClient(app, follow_redirects=True)


def _set_up_state(state: str) -> None:
    from detection.risk_score import RiskScore
    from detection.storage import save_scores

    seeded = _SEEDED_STATE.match(state)
    if seeded:
        save_scores(
            [
                RiskScore(
                    wallet=seeded.group(1),
                    asset_pair="XLM/USDC",
                    score=85,
                    benford_flag=True,
                    ml_flag=True,
                    confidence=90,
                    disputed=False,
                    timestamp=datetime.now(timezone.utc),
                )
            ]
        )
    elif not _EMPTY_STATE.match(state):
        raise AssertionError(f"Unknown provider state: {state!r}")


def _interactions():
    for pact_file in PACT_FILES:
        pact = json.loads(pact_file.read_text())
        for interaction in pact["interactions"]:
            yield pytest.param(
                interaction,
                id=f"{pact['consumer']['name']}::{interaction['description']}",
            )


def test_every_sdk_publishes_a_contract():
    consumers = {json.loads(p.read_text())["consumer"]["name"] for p in PACT_FILES}
    assert consumers == {
        "ledgerlens-sdk-typescript",
        "ledgerlens-sdk-go",
        "ledgerlens-sdk-rust",
    }


@pytest.mark.contract
@pytest.mark.parametrize("interaction", list(_interactions()))
def test_api_satisfies_sdk_contract(api_client, interaction):
    from detection.api_key_store import create_api_key

    _set_up_state(interaction["providerState"])
    request = interaction["request"]
    key = create_api_key(scopes=["read:scores"])["plaintext_key"]
    url = request["path"] + (f"?{request['query']}" if request.get("query") else "")

    response = api_client.request(request["method"], url, headers={"X-LedgerLens-Api-Key": key})

    expected = interaction["response"]
    assert response.status_code == expected["status"], (
        f"{interaction['description']}: expected HTTP {expected['status']}, "
        f"got {response.status_code} — {response.text[:300]}"
    )
    if "body" in expected:
        errors = shape_mismatches(expected["body"], response.json())
        assert not errors, f"{interaction['description']}: contract broken:\n" + "\n".join(errors)


def test_breaking_change_is_detected():
    """A renamed or retyped field must be reported as a contract violation."""
    expected = {"scores": [{"wallet": "G", "score": 1, "disputed": False}]}
    assert (
        shape_mismatches(expected, {"scores": [{"wallet": "G", "score": 1, "disputed": False}]})
        == []
    )
    assert shape_mismatches(expected, {"scores": [{"wallet": "G", "risk": 1, "disputed": False}]})
    assert shape_mismatches(
        expected, {"scores": [{"wallet": "G", "score": "1", "disputed": False}]}
    )
    assert shape_mismatches(expected, {"scores": []})
