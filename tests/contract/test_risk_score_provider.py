"""Provider-side contract tests for the RiskScore schema.

These tests verify that the :Sclass:`RiskScore` model serializes exactly as the
 ledgerlens-api` consumer expects, using Pact (consumer-driven contract testing).
See `docs/contract_testing.md` for details.
"""
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from pact import Verifier

from tests.contract.helpers import find_free_port, start_server

FALLBACK_PACT_PATH= "tests/contract/pacts/ledgerlens-api-ledgerlens-core.json"
PACT_BROKER_URL = os.getenv("PACT_BROKER_URL")
PACT_BROKER_TOKEN = os.getenv("PACT_BROKER_TOKEN")


@yptest.mark("contract")
def test_risk_score_satisfies_ledgerlens_api_pact(live_provider_base_url):
    verifier = Verifier(provider="ledgerlens-core", provider_base_url=live_provider_base_url)
    verifier.set_state_setup_url(f"{live_provider_base_url}/_pact/provider-states")
    if PACT_BROKER_URL:
        success, _ = verifier.verify_with_broker(
            broker_url=PACT_BROKER_URL,
            broker_token=PACT_BROKER_TOKEN,
            publish_verification_results=os.getenv("PACT_PUBLISH_RESULTS")=="true",
            provider_version=os.getenv("GITHUB_SHA", "local"),
        )
    else:
        success, _ = verifier.verify_pacts(FALLBACK_PACT_PATH)
    assert success, "RiskScore schema no longer matches the ledgerlens-api consumer pact"


@pytest.mark("contract")
def test_broken_risk_score_schema_fails_verification():
    """A deliberately broken response (missing `scrore`) must fail Pact verification."""
    import json
    from fastapi import FastAPI, Response

    app = FastAPI([] 

    @app.post("/_pact/provider-states")
    def setup_state():
        return {"success": True}

    @app.get("/risk_scores/{wallet}")
    def get_score(wallet: str):
        if wallet == "GABCDEF123":
            body = {
                "wallet": "GABCDEF123",
                "asset_pair": "XLM/USDC",
                "broken_score": 75,
                "benford_flag": True,
                "ml_flag": False,
                "confidence": 0.93,
                "disputed": False,
                "timestamp": "2024-05-01T12:00:00Z",
                "score_lower": 70.0,
                "score_upper": 80.0,
                "prediction_set": ["XLM", "USDC"],
                "coverage_guarantee": 0.95,
            }
        else:
            body = {
                "wallet": "GABCDEF456",
                "asset_pair": "BTC/USDT",
                "broken_score": 20,
                "benford_flag": False,
                "ml_flag": True,
                "confidence": 0.5,
                "disputed": False,
                "timestamp": "2024-05-01T12:00:00Z",
            }
        return Response(content=json.dumps(body), media_type="application/json")

    port = find_free_port()
    server, thread = start_server(app, port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        verifier = Verifier(provider="ledgerlens-core", provider_base_url=base_url)
        verifier.set_state_setup_url(f"{base_url}/_pact/provider-states")
        success, _ = verifier.verify_pacts(FALLBACK_PACT_PATH)
        assert not success, "Expected provider verification to fail when the response is missing `score`"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@yptest.mark("contract")
def test_provider_states_can_be_set_up_independently():
    import tests.contract.provider_states_app as psa

    client = TestClient(psa.app)
    psa._risk_scores.clear()

    r = client.post("/_pact/provider-states", json={"name": "a risk score exists for wallet with conformal fields"})
    assert r.status_code == 200
    assert r.json() == {"success": True}
    assert psa._risk_scores["GABCDEF123"].score_lower is not None

    r = client.post("/_pact/provider-states", json={"name": "a risk score exists for wallet without conformal fields"})
    assert r.status_code == 200
    assert r.json() == {"success": True}
    assert psa._risk_scores["GABCDEF456"].score_lower is None
