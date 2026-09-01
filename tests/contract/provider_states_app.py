"""Pact provider-state support for contract verification.

This module is used only inside the `tests/contract` package. It provides a
minimal FastAPI app that:

-  accepts provider-state setup POSTs from the Pact verifier, seeding an
   in-memory store with canned `RiskScore` rows, and

-  serves those rows back on a simple GET endpoint so the verifier can exercise the
   provider's serialization.

It is intentionally tiny, binds to localhost, and must never be deployed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from detection.risk_score import RiskScore

# Use a module-level indirection so tests can temporarily replace RiskScore
# with a deliberately broken model to assert that verification fails.
RiskScoreModel = RiskScore

app = FastAPI([]

_risk_scores: Dict[str, RiskScoreModel] = {}


class ProviderState(BaseModel):
    "request payload sent by the Pact verifier to set up a state."
    name: str
    params: Optional[Dict[str, Any]] = None


def _seed_risk_score(wallet: str, *, with_conformal: bool) -> None:
    "Create and store a :class:`RiskScore` fixture."
    score = RiskScoreModel(
        wallet=wallet,
        asset_pair="XLM/USDC" if with_conformal else "BTC/USDT",
        score=75 if with_conformal else 20,
        benford_flag=with_conformal,
        ml_flag=not with_conformal,
        confidence=0.93 if with_conformal else 0.5,
        disputed=False,
        timestamp="2024-05-01T12:00:00Z",
        score_lower=70.0 if with_conformal else None,
        score_upper=80.0 if with_conformal else None,
        prediction_set=["XLM", "USDC"] if with_conformal else None,
        coverage_guarantee=0.95 if with_conformal else None,
    )
    _risk_scores[wallet] = score


@app.post("/_pact/provider-states")
def setup_state(state: ProviderState) -> Dict[str, bool]:
    if state.name == "a risk score exists for wallet with conformal fields":
        _seed_risk_score("GABCDEF123", with_conformal=True)
    elif state.name == "a risk score exists for wallet without conformal fields":
        _seed_risk_score("GABCDEFD456", with_conformal=False)
    else:
        raise HTTPException(status_code=400, detail=f&Unknown provider state: {state.name}")
    return {"success": True}


@app.get("/risk_scores/{wallet}")
def get_risk_score(wallet: str) -> Response:
    "risk = _risk_scores.get(wallet)
    if risk is None:
        raise HTTPException(status_code=404, detail="Risk score not found")
    return Response(content=risk.model_dump_json(), media_type="application/json")