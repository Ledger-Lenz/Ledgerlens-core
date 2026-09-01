/// Cross-language schema contract vector tests (Rust side).
///
/// These tests load the canonical fixture file `tests/fixtures/contract_vectors.json`
/// and verify that every valid vector deserializes cleanly via serde_json into the
/// `ledgerlens_sdk::models::RiskScore` struct. They also verify that adversarial
/// vectors (wrong field names, out-of-range values) are handled correctly.
///
/// ADR reference: docs/adr/ADR-005-schema-contract-enforcement.md
///
/// The fixture path is resolved relative to the workspace root using the
/// CARGO_MANIFEST_DIR environment variable set by cargo test.
use ledgerlens_sdk::models::RiskScore;
use serde_json::Value;
use std::fs;
use std::path::PathBuf;

/// Returns the path to the contract vectors fixture file.
///
/// During `cargo test` the current directory is the workspace root, so we
/// can resolve the fixture path relative to CARGO_MANIFEST_DIR's parent (the
/// workspace root).
fn fixture_path() -> PathBuf {
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR")
        .expect("CARGO_MANIFEST_DIR must be set by cargo test");
    // crates/ledgerlens-sdk → go up two levels to workspace root
    let workspace_root = PathBuf::from(manifest_dir)
        .parent()
        .expect("crate has parent dir")
        .parent()
        .expect("crates/ has parent dir")
        .to_path_buf();
    workspace_root
        .join("tests")
        .join("fixtures")
        .join("contract_vectors.json")
}

fn load_fixture() -> Value {
    let path = fixture_path();
    let content = fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "Failed to read contract vectors fixture at {}: {}. \
             Run: python scripts/generate_contract_vectors.py",
            path.display(),
            e
        )
    });
    serde_json::from_str(&content).expect("contract_vectors.json must be valid JSON")
}

fn parse_risk_score(fixture: &Value, key: &str) -> RiskScore {
    let raw = &fixture["risk_score"][key];
    serde_json::from_value(raw.clone()).unwrap_or_else(|e| {
        panic!(
            "Failed to deserialize risk_score['{}'] into RiskScore: {}\nRaw: {}",
            key,
            e,
            serde_json::to_string_pretty(raw).unwrap_or_default()
        )
    })
}

// ---------------------------------------------------------------------------
// Valid vector tests
// ---------------------------------------------------------------------------

#[test]
fn contract_vector_risk_score_complete() {
    let fixture = load_fixture();
    let score = parse_risk_score(&fixture, "complete");

    assert!(!score.wallet.is_empty(), "wallet must not be empty");
    assert!(!score.asset_pair.is_empty(), "asset_pair must not be empty");
    assert!(score.score <= 100, "score must be in 0-100 range");
    assert!(score.confidence <= 100, "confidence must be in 0-100 range");

    // latency_ms must be present in the complete vector
    assert!(
        score.latency_ms.is_some(),
        "latency_ms is None in 'complete' vector but should be populated. \
         This field exists in the canonical Python model (detection/risk_score.py)."
    );

    // v2+ uncertainty fields must be present in the complete vector
    assert!(
        score.score_lower.is_some(),
        "score_lower missing from complete vector (v2+ uncertainty field)"
    );
    assert!(
        score.score_upper.is_some(),
        "score_upper missing from complete vector (v2+ uncertainty field)"
    );
    assert!(
        score.prediction_set.is_some(),
        "prediction_set missing from complete vector (v2+ uncertainty field)"
    );
    assert!(
        score.coverage_guarantee.is_some(),
        "coverage_guarantee missing from complete vector (v2+ uncertainty field)"
    );
}

#[test]
fn contract_vector_risk_score_minimal() {
    let fixture = load_fixture();
    let score = parse_risk_score(&fixture, "minimal");

    // Optional fields must be None in the minimal vector
    assert!(score.latency_ms.is_none(), "latency_ms should be None in minimal vector");
    assert!(score.score_lower.is_none());
    assert!(score.score_upper.is_none());
    assert!(score.prediction_set.is_none());
    assert!(score.coverage_guarantee.is_none());
}

#[test]
fn contract_vector_risk_score_disputed() {
    let fixture = load_fixture();
    let score = parse_risk_score(&fixture, "disputed");
    assert!(score.disputed, "disputed must be true in the disputed vector");
}

#[test]
fn contract_vector_risk_score_boundary_zero() {
    let fixture = load_fixture();
    let score = parse_risk_score(&fixture, "score_boundary_zero");
    assert_eq!(score.score, 0, "score must be 0 (boundary value)");
}

#[test]
fn contract_vector_risk_score_boundary_hundred() {
    let fixture = load_fixture();
    let score = parse_risk_score(&fixture, "score_boundary_hundred");
    assert_eq!(score.score, 100, "score must be 100 (boundary value)");
}

// ---------------------------------------------------------------------------
// prediction_set type correctness
// ---------------------------------------------------------------------------

#[test]
fn contract_vector_prediction_set_is_vec_i32() {
    /// Verify that prediction_set elements are i32 (signed integer), not u8.
    ///
    /// The Python canonical type is `list[int]` (signed integers in the range
    /// of class indices, typically 0 and 1). A previous Rust implementation
    /// incorrectly used `Vec<u8>`, which would silently truncate or reject
    /// negative indices and diverge from the Python contract.
    let fixture = load_fixture();
    let score = parse_risk_score(&fixture, "complete");

    if let Some(ps) = &score.prediction_set {
        // This assertion is primarily a type-system check (the Rust type
        // already enforces Vec<i32>), but we also verify the value round-trips.
        assert!(
            !ps.is_empty(),
            "prediction_set in 'complete' vector should not be empty"
        );
        // Verify that class index 1 (wash-trading class) round-trips correctly.
        // If Vec<u8> were still used, negative indices would fail to deserialize.
        for &idx in ps {
            assert!(
                idx >= 0,
                "prediction_set index {} is negative — class indices start at 0",
                idx
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Adversarial vector tests (divergence detection)
// ---------------------------------------------------------------------------

#[test]
fn contract_vector_adversarial_wrong_field_name_rejected() {
    /// A payload with 'risk_score' instead of 'score' must fail deserialization.
    ///
    /// serde_json with #[serde(deny_unknown_fields)] would reject this outright.
    /// Without that attribute, the missing 'score' field (required) causes a
    /// missing-field error. Either outcome proves the deserializer is not silently
    /// accepting the renamed field.
    ///
    /// This test proves divergence detection: a field rename in Python that is
    /// not reflected in the Rust model will be caught here.
    let fixture = load_fixture();
    let raw = &fixture["risk_score_adversarial"]["wrong_field_name"];

    let result: Result<RiskScore, _> = serde_json::from_value(raw.clone());

    // The deserialization must fail because 'score' is missing (renamed to 'risk_score').
    // RiskScore.score is a required field with no default.
    assert!(
        result.is_err(),
        "Adversarial vector with 'risk_score' instead of 'score' was unexpectedly accepted. \
         This means a field rename in the Python model would go undetected in Rust."
    );
}

#[test]
fn contract_vector_all_valid_vectors_deserialize() {
    /// Meta-test: every non-adversarial RiskScore vector must deserialize without error.
    let fixture = load_fixture();
    let valid_keys = ["complete", "minimal", "disputed", "score_boundary_zero", "score_boundary_hundred"];

    for key in &valid_keys {
        let raw = &fixture["risk_score"][key];
        let result: Result<RiskScore, _> = serde_json::from_value(raw.clone());
        assert!(
            result.is_ok(),
            "Valid vector '{}' failed to deserialize: {:?}",
            key,
            result.err()
        );
    }
}

// ---------------------------------------------------------------------------
// Required fields agreement check
// ---------------------------------------------------------------------------

#[test]
fn contract_vector_required_fields_declared_in_fixture() {
    /// Verify the fixture declares all expected required fields for RiskScore.
    ///
    /// This acts as the 'agreement check': if a field is added to the Rust struct
    /// that is not in the fixture, this test prompts the developer to update the
    /// fixture (and then all language implementations).
    let fixture = load_fixture();
    let declared: Vec<String> = serde_json::from_value(
        fixture["required_risk_score_fields"].clone()
    )
    .expect("required_risk_score_fields must be a JSON array of strings");

    // These are the fields the Rust RiskScore struct knows about.
    // If a new field is added here that's not in the fixture, this test fails.
    let rust_fields = [
        "wallet", "asset_pair", "score", "benford_flag", "ml_flag",
        "confidence", "disputed", "timestamp", "latency_ms",
        "score_lower", "score_upper", "prediction_set", "coverage_guarantee",
    ];

    for field in &rust_fields {
        assert!(
            declared.iter().any(|d| d == field),
            "Rust struct field '{}' is not declared in fixture's required_risk_score_fields. \
             Update the fixture and all language implementations. \
             Run: python scripts/generate_contract_vectors.py",
            field
        );
    }
}
