//! API contract tests: assert that the SDK models can deserialize the response
//! shapes the API publishes in `docs/openapi.json`.
//!
//! `docs/openapi.json` is exported from `api/` and CI (`schema.yml`) fails if it
//! drifts from the running API, so any API change that breaks the SDK surfaces
//! here before release. See "Semver Policy" in the crate README.

use ledgerlens_sdk::models::RiskScore;
use serde_json::{json, Map, Value};

fn openapi() -> Value {
    serde_json::from_str(include_str!("../../../docs/openapi.json")).expect("valid openapi.json")
}

fn schema(name: &str) -> Value {
    openapi()["components"]["schemas"][name].clone()
}

/// Build a representative value for a JSON-schema property.
fn sample(prop: &Value) -> Value {
    if let Some(variants) = prop.get("anyOf").and_then(Value::as_array) {
        let non_null = variants
            .iter()
            .find(|v| v["type"] != "null")
            .expect("anyOf has a non-null variant");
        return sample(non_null);
    }
    match prop["type"].as_str() {
        Some("string") if prop["format"] == "date-time" => json!("2026-01-01T00:00:00Z"),
        Some("string") => json!("GABC"),
        Some("integer") => json!(prop["minimum"].as_f64().unwrap_or(1.0) as i64 + 1),
        Some("number") => json!(0.5),
        Some("boolean") => json!(true),
        Some("array") => json!([sample(&prop["items"])]),
        other => panic!("unsupported schema type in contract test: {other:?}"),
    }
}

fn payload(schema: &Value, only_required: bool) -> Value {
    let required: Vec<&str> = schema["required"]
        .as_array()
        .map(|r| r.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let mut out = Map::new();
    for (name, prop) in schema["properties"].as_object().expect("properties") {
        if !only_required || required.contains(&name.as_str()) {
            out.insert(name.clone(), sample(prop));
        }
    }
    Value::Object(out)
}

#[test]
fn risk_score_accepts_minimal_api_response() {
    // Fails if the API makes optional (or removes) a field the SDK requires.
    let body = payload(&schema("RiskScore"), true);
    serde_json::from_value::<RiskScore>(body.clone())
        .unwrap_or_else(|e| panic!("SDK RiskScore rejects minimal API response {body}: {e}"));
}

#[test]
fn risk_score_accepts_full_api_response() {
    // Fails if the API changes the type of any field the SDK models.
    let body = payload(&schema("RiskScore"), false);
    serde_json::from_value::<RiskScore>(body.clone())
        .unwrap_or_else(|e| panic!("SDK RiskScore rejects full API response {body}: {e}"));
}

#[test]
fn risk_score_accepts_null_optional_fields() {
    let schema = schema("RiskScore");
    let mut body = payload(&schema, false);
    for (name, prop) in schema["properties"].as_object().unwrap() {
        let nullable = prop["anyOf"]
            .as_array()
            .is_some_and(|v| v.iter().any(|t| t["type"] == "null"));
        if nullable {
            body[name] = Value::Null;
        }
    }
    serde_json::from_value::<RiskScore>(body).expect("nullable API fields must map to Option");
}

#[test]
fn risk_score_fields_all_exist_in_api() {
    // Fails if the API drops a field the SDK exposes to consumers.
    let schema = schema("RiskScore");
    let api_fields = schema["properties"].as_object().unwrap();
    let full: RiskScore = serde_json::from_value(payload(&schema, false)).unwrap();
    let sdk = serde_json::to_value(full).unwrap();
    for field in sdk.as_object().unwrap().keys() {
        assert!(
            api_fields.contains_key(field),
            "SDK field `{field}` is not in the API RiskScore schema (docs/openapi.json)"
        );
    }
}
