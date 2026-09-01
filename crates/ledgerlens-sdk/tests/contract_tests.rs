use ledgerlens_sdk::models::RiskScore;
use serde_json::Value;

#[test]
fn test_risk_score_contract_schema() {
    let fixture_str = include_str!("../../../tests/fixtures/schemas/risk_score_v1.json");
    let original_json: Value = serde_json::from_str(fixture_str).unwrap();
    
    // Parse from JSON fixture
    let score: RiskScore = serde_json::from_value(original_json.clone()).unwrap();
    
    assert_eq!(score.wallet, "GBXGQJWVN5C3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3Z3");
    assert_eq!(score.score, 85);
    assert_eq!(score.benford_flag, true);
    
    // Serialize back and compare
    let serialized_json = serde_json::to_value(&score).unwrap();
    
    // We assert that everything in the fixture is present in the serialized output
    for (key, val) in original_json.as_object().unwrap() {
        assert_eq!(
            serialized_json.get(key), 
            Some(val),
            "Mismatch on key '{}'", key
        );
    }
}
