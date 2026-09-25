//! Cross-SDK conformance runner (Rust side).
//!
//! Executes every case in `tests/contract/conformance/cases.json` against the
//! shared reference server. Set `LEDGERLENS_CONFORMANCE_URL` (e.g.
//! `http://127.0.0.1:8787`) to run; skipped otherwise. See
//! `tests/contract/conformance/README.md`.

use ledgerlens_sdk::{LedgerLensClient, LedgerLensError};
use serde_json::{json, Value};

fn error_status(err: &LedgerLensError) -> Option<u16> {
    match err {
        LedgerLensError::Unauthorized(_) => Some(401),
        LedgerLensError::NotFound(_) => Some(404),
        LedgerLensError::RateLimited(_) => Some(429),
        LedgerLensError::Api { status_code, .. } => Some(*status_code),
        _ => None,
    }
}

async fn run_case(client: &LedgerLensClient, case: &Value) -> Value {
    let result = match case["operation"].as_str().unwrap() {
        "health" => client.health().await.map(|h| json!({ "status": h.status })),
        "list_scores" => client
            .get_scores(case["args"]["asset_pair"].as_str())
            .await
            .map(|scores| {
                json!({
                    "wallets": scores.iter().map(|s| s.wallet.clone()).collect::<Vec<_>>(),
                    "scores": scores.iter().map(|s| s.score).collect::<Vec<_>>(),
                })
            }),
        op => panic!("unknown operation {op}"),
    };
    match result {
        Ok(ok) => json!({ "ok": ok }),
        Err(err) => match error_status(&err) {
            Some(status) => json!({ "error": { "status": status } }),
            None => panic!("case {}: transport error {err:?}", case["id"]),
        },
    }
}

#[tokio::test]
async fn conformance() {
    let Ok(base_url) = std::env::var("LEDGERLENS_CONFORMANCE_URL") else {
        eprintln!("LEDGERLENS_CONFORMANCE_URL not set; skipping conformance suite");
        return;
    };
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/contract/conformance/cases.json"
    );
    let doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let client = LedgerLensClient::new(base_url.clone(), None);
    let http = reqwest::Client::new();

    let mut failures = Vec::new();
    for case in doc["cases"].as_array().unwrap() {
        let id = case["id"].as_str().unwrap();
        let sel = http
            .post(format!("{base_url}/__conformance/select?case={id}"))
            .send()
            .await
            .unwrap();
        assert!(sel.status().is_success(), "failed to select case {id}");
        let got = run_case(&client, case).await;
        if got != case["expect"] {
            failures.push(format!("{id}: expected {}, got {got}", case["expect"]));
        }
    }
    assert!(
        failures.is_empty(),
        "conformance failures:\n{}",
        failures.join("\n")
    );
}
