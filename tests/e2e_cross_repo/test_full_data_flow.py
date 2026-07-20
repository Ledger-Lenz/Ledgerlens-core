
import pytest
import requests
from urllib.parse import urljoin

from detection.risk_score import RiskScore


def test_risk_score_schema_drift(api_base_url):
    """Test that RiskScore schema matches between core and api's OpenAPI spec."""
    # Get core's schema
    core_schema = RiskScore.model_json_schema()

    # Get api's OpenAPI spec
    response = requests.get(urljoin(api_base_url, "/openapi.json"), timeout=10)
    response.raise_for_status()
    api_spec = response.json()

    # Find api's RiskScore schema in components.schemas
    # (Adjust the schema name to match ledgerlens-api's actual name)
    api_schema = api_spec["components"]["schemas"].get("RiskScore", {})

    # Compare fields
    core_fields = set(core_schema["properties"].keys())
    api_fields = set(api_schema.get("properties", {}).keys())
    assert core_fields == api_fields, (
        f"Schema field mismatch! Core has {core_fields}, API has {api_fields}"
    )


def test_score_retrieved_via_api(api_base_url):
    """Test that a score computed by core is retrievable via api's /score endpoint."""
    # TODO: Implement a test that computes a score in core,
    # sends it to api, then retrieves it
    pytest.skip("Not yet implemented.")


def test_score_on_chain(api_base_url):
    """Test that a score above threshold is forwarded to the Soroban contract."""
    pytest.skip("Not yet implemented.")

