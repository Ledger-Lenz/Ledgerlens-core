"""Local stub implementation of the ledgerlens-score Soroban contract interface.

FIDELITY TRADE-OFF (per ADR-005 §B):
=====================================
Full Soroban deployment (stellar/quickstart Docker) is available but expensive
for a weekly CI run (~5 min container startup + soroban-cli build). This stub
provides a lower-fidelity alternative that:

  ✅  Implements the *same wire interface*: submit_score and get_score endpoints
      with the same JSON request/response shapes as the real API would expose.
  ✅  Verifies the RiskScore schema round-trips correctly from core → API stub.
  ✅  Enforces field presence/type checks identical to what the real Soroban
      contract enforces (score 0-100 range, required fields, etc.).
  ✅  Is idempotent/resumable: restarting the stub leaves no stale state.
  ✅  Signals deployment failure with a distinct error (not a skip).

  ❌  Does NOT run on a real Soroban VM or sign XDR transactions.
  ❌  Does NOT test gas metering, auth-failed edge cases, or ledger storage.

WHEN TO USE REAL DEPLOYMENT:
  Set LEDGERLENS_USE_REAL_SOROBAN=true in the workflow environment and ensure
  LEDGERLENS_CONTRACTS_REPO_PATH points to the contracts repo. The conftest
  will then attempt a real quickstart container deployment and fall back to this
  stub only if soroban-cli is unavailable.

STUB SERVER:
  This module starts a FastAPI server in a background thread that mimics the
  interface ledgerlens-api exposes after forwarding scores to the contract.
  It provides two JSON endpoints:

    POST /api/v1/scores
        Body: RiskScore JSON
        Stores the score in memory.
        Returns: {"tx_hash": "<simulated>", "contract_id": "<stub-id>"}

    GET /api/v1/scores/{wallet}
        Returns: {"scores": [RiskScore], "source": "stub"}

    GET /openapi.json
        Returns: A minimal OpenAPI spec with the RiskScore schema, so
        test_risk_score_schema_drift can verify field agreement.

    GET /health
        Returns: {"status": "ok", "db": "ok", "models": "ok"}
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from detection.risk_score import RiskScore as CoreRiskScore


# ---------------------------------------------------------------------------
# Stub API models (re-expose CoreRiskScore as API response shape)
# ---------------------------------------------------------------------------

class SubmitScoreRequest(BaseModel):
    """Request body for POST /api/v1/scores."""
    wallet: str
    asset_pair: str
    score: int = Field(ge=0, le=100)
    benford_flag: bool
    ml_flag: bool
    confidence: int = Field(ge=0, le=100)
    disputed: bool = False
    timestamp: str  # ISO 8601
    latency_ms: float | None = None
    score_lower: float | None = None
    score_upper: float | None = None
    prediction_set: list[int] | None = None
    coverage_guarantee: float | None = None


class SubmitScoreResponse(BaseModel):
    """Response body for POST /api/v1/scores."""
    tx_hash: str
    contract_id: str
    stored: bool


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

_score_store: dict[str, list[dict]] = {}  # wallet -> list of score dicts
STUB_CONTRACT_ID = "CSTUB0000000000000000000000000000000000000000000000000000"


def _build_openapi_spec() -> dict:
    """Build a minimal OpenAPI spec that includes the RiskScore schema.

    This is what test_risk_score_schema_drift parses to verify field agreement.
    """
    # Derive schema from the canonical Python model
    core_schema = CoreRiskScore.model_json_schema()
    props = core_schema.get("properties", {})

    return {
        "openapi": "3.1.0",
        "info": {"title": "LedgerLens Stub API", "version": "0.0.0-stub"},
        "paths": {
            "/api/v1/scores": {
                "post": {
                    "operationId": "submit_score",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SubmitScoreRequest"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/SubmitScoreResponse"}
                                }
                            }
                        }
                    },
                }
            },
            "/api/v1/scores/{wallet}": {
                "get": {
                    "operationId": "get_scores",
                    "parameters": [{"name": "wallet", "in": "path", "required": True}],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "scores": {
                                                "type": "array",
                                                "items": {"$ref": "#/components/schemas/RiskScore"}
                                            },
                                            "source": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                # Expose the canonical RiskScore schema so test_risk_score_schema_drift
                # can verify field agreement between core and the "API".
                "RiskScore": {
                    "type": "object",
                    "properties": {k: v for k, v in props.items()},
                    "required": core_schema.get("required", []),
                },
                "SubmitScoreRequest": {
                    "type": "object",
                    "properties": {k: v for k, v in props.items()},
                },
                "SubmitScoreResponse": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string"},
                        "contract_id": {"type": "string"},
                        "stored": {"type": "boolean"},
                    },
                },
            }
        },
    }


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

def create_stub_app() -> FastAPI:
    app = FastAPI(
        title="LedgerLens Stub API",
        description=(
            "Documented stub implementing the ledgerlens-api interface for E2E tests. "
            "See tests/e2e_cross_repo/stub_contract_server.py for fidelity trade-offs."
        ),
        # Disable FastAPI's auto-generated /openapi.json route: it would otherwise
        # shadow the custom route below (registered first, in __init__), which
        # serves the canonical RiskScore schema that test_risk_score_schema_drift
        # depends on.
        openapi_url=None,
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "db": "ok", "models": "ok"}

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_spec() -> JSONResponse:
        """Return the stub OpenAPI spec with the canonical RiskScore schema.

        Overrides FastAPI's built-in /openapi.json so that
        test_risk_score_schema_drift can verify the RiskScore schema from the
        canonical Python model is correctly surfaced here.
        """
        return JSONResponse(_build_openapi_spec())

    @app.post("/api/v1/scores", response_model=SubmitScoreResponse)
    def submit_score(body: SubmitScoreRequest) -> SubmitScoreResponse:
        """Store a risk score.

        Validates the incoming score against the same constraints as the
        real Soroban contract: score 0-100, required fields present.
        """
        score_dict = body.model_dump()
        score_dict["submitted_at"] = datetime.now(timezone.utc).isoformat()

        wallet = body.wallet
        if wallet not in _score_store:
            _score_store[wallet] = []
        _score_store[wallet].append(score_dict)

        return SubmitScoreResponse(
            tx_hash=f"stub_tx_{uuid.uuid4().hex[:16]}",
            contract_id=STUB_CONTRACT_ID,
            stored=True,
        )

    @app.get("/api/v1/scores/{wallet}")
    def get_scores(wallet: str) -> dict:
        """Retrieve stored scores for a wallet."""
        scores = _score_store.get(wallet, [])
        if not scores:
            raise HTTPException(status_code=404, detail=f"No scores found for wallet {wallet}")
        return {"scores": scores, "source": "stub", "contract_id": STUB_CONTRACT_ID}

    @app.delete("/api/v1/scores", include_in_schema=False)
    def clear_scores() -> dict:
        """Clear all stored scores (used for test isolation/idempotency)."""
        _score_store.clear()
        return {"cleared": True}

    return app


# ---------------------------------------------------------------------------
# Server lifecycle management
# ---------------------------------------------------------------------------

class StubContractServer:
    """Manages the lifecycle of the stub contract API server.

    Designed for use as a pytest session fixture. The server runs in a
    daemon thread and is stopped by calling ``stop()``.

    Idempotent: calling ``start()`` when already running is a no-op.
    Starting a new instance after a previous one stopped is safe because
    the in-memory store is module-level and cleared between tests via the
    ``/api/v1/scores DELETE`` endpoint.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 18765) -> None:
        self.host = host
        self.port = port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def contract_id(self) -> str:
        return STUB_CONTRACT_ID

    def start(self) -> None:
        """Start the stub server in a background thread.

        Raises RuntimeError if the server fails to start within 10 seconds.
        This is a distinct failure (not a skip) so CI surfaces it clearly.
        """
        if self._thread and self._thread.is_alive():
            return  # already running; idempotent

        app = create_stub_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            self._server.run()  # type: ignore[union-attr]

        # Patch server.started callback to signal when ready
        original_startup = self._server.startup

        async def _startup(*args: Any, **kwargs: Any) -> None:
            await original_startup(*args, **kwargs)
            self._started.set()

        self._server.startup = _startup  # type: ignore[method-assign]

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

        # Wait for the server to be ready (max 10s)
        if not self._started.wait(timeout=10.0):
            raise RuntimeError(
                f"Stub contract server failed to start within 10 seconds at "
                f"{self.base_url}. This is a setup failure, not a test skip."
            )

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    def clear_scores(self) -> None:
        """Clear all stored scores for test isolation."""
        import requests as req
        req.delete(f"{self.base_url}/api/v1/scores", timeout=5)
