import json
from pathlib import Path

from detection.risk_score import RiskScore
from ingestion.data_models import Asset, Trade

def test_risk_score_contract_schema():
    fixture_path = Path(__file__).parent / "fixtures" / "schemas" / "risk_score_v1.json"
    with open(fixture_path, "r") as f:
        data = json.load(f)
    
    score = RiskScore.model_validate(data)
    assert score.wallet == "GBXGQJWVN5C3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3"
    assert score.score == 85
    
    dumped = score.model_dump(mode="json", exclude_none=True)
    for key, val in data.items():
        assert dumped[key] == val

def test_asset_contract_schema():
    fixture_path = Path(__file__).parent / "fixtures" / "schemas" / "asset_v1.json"
    with open(fixture_path, "r") as f:
        data = json.load(f)
    
    asset = Asset.model_validate(data)
    assert asset.code == "XLM"
    
    dumped = asset.model_dump(mode="json")
    for key, val in data.items():
        assert dumped[key] == val

def test_trade_contract_schema():
    fixture_path = Path(__file__).parent / "fixtures" / "schemas" / "trade_v1.json"
    with open(fixture_path, "r") as f:
        data = json.load(f)
    
    trade = Trade.model_validate(data)
    assert trade.id == "trade-12345"
    assert trade.base_asset.code == "XLM"
    
    dumped = trade.model_dump(mode="json")
    for key, val in data.items():
        assert dumped.get(key) == val

