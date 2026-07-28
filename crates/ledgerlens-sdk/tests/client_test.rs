use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use ledgerlens_sdk::LedgerLensClient;

/// Helper to create a mock client pointing at a wiremock server.
async fn setup_mock() -> (MockServer, LedgerLensClient) {
    let mock_server = MockServer::start().await;
    let client = LedgerLensClient::new(mock_server.uri(), Some("test-key".into()));
    (mock_server, client)
}

#[tokio::test]
async fn test_get_score_happy_path() {
    let (server, client) = setup_mock().await;

    let body = serde_json::json!({
        "scores": [
            {
                "wallet": "GA1234567890ABCDEF",
                "asset_pair": "XLM/USDC",
                "score": 42,
                "benford_flag": true,
                "ml_flag": false,
                "confidence": 85,
                "disputed": false,
                "timestamp": "2024-01-15T10:30:00Z",
                "score_lower": null,
                "score_upper": null,
                "prediction_set": null,
                "coverage_guarantee": null
            }
        ],
        "cross_chain_links": []
    });

    Mock::given(method("GET"))
        .and(path("/v1/scores/GA1234567890ABCDEF"))
        .and(header("X-LedgerLens-API-Key", "test-key"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&body))
        .expect(1)
        .mount(&server)
        .await;

    let resp = client.get_score("GA1234567890ABCDEF").await.unwrap();
    assert_eq!(resp.scores.len(), 1);
    assert_eq!(resp.scores[0].wallet, "GA1234567890ABCDEF");
    assert_eq!(resp.scores[0].score, 42);
    assert!(resp.scores[0].benford_flag);
    assert!(!resp.scores[0].ml_flag);
    assert_eq!(resp.scores[0].confidence, 85);
    assert!(resp.cross_chain_links.is_empty());
}

#[tokio::test]
async fn test_get_scores_happy_path() {
    let (server, client) = setup_mock().await;

    let body = serde_json::json!([
        {
            "wallet": "GA1234567890ABCDEF",
            "asset_pair": "XLM/USDC",
            "score": 42,
            "benford_flag": true,
            "ml_flag": false,
            "confidence": 85,
            "disputed": false,
            "timestamp": "2024-01-15T10:30:00Z",
            "score_lower": null,
            "score_upper": null,
            "prediction_set": null,
            "coverage_guarantee": null
        }
    ]);

    Mock::given(method("GET"))
        .and(path("/v1/scores"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&body))
        .expect(1)
        .mount(&server)
        .await;

    let resp = client.get_scores(None).await.unwrap();
    assert_eq!(resp.len(), 1);
    assert_eq!(resp[0].wallet, "GA1234567890ABCDEF");
}

#[tokio::test]
async fn test_get_scores_with_asset_pair() {
    let (server, client) = setup_mock().await;

    let body = serde_json::json!([]);

    Mock::given(method("GET"))
        .and(path("/v1/scores"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&body))
        .expect(1)
        .mount(&server)
        .await;

    let resp = client.get_scores(Some("XLM/USDC")).await.unwrap();
    assert!(resp.is_empty());
}

#[tokio::test]
async fn test_get_rings_happy_path() {
    let (server, client) = setup_mock().await;

    let body = serde_json::json!([
        {
            "ring_id": "ring-001",
            "asset_pair": "XLM/USDC",
            "wallets": ["GA1", "GA2", "GA3"],
            "total_volume": 50000.0,
            "trade_count": 150,
            "detected_at": "2024-01-15T10:30:00Z"
        }
    ]);

    Mock::given(method("GET"))
        .and(path("/v1/rings"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&body))
        .expect(1)
        .mount(&server)
        .await;

    let resp = client.get_rings().await.unwrap();
    assert_eq!(resp.len(), 1);
    assert_eq!(resp[0].ring_id, "ring-001");
    assert_eq!(resp[0].wallets.len(), 3);
}

#[tokio::test]
async fn test_health_happy_path() {
    let (server, client) = setup_mock().await;

    let body = serde_json::json!({
        "status": "healthy",
        "db": "connected",
        "models": "loaded",
        "circuits": null
    });

    Mock::given(method("GET"))
        .and(path("/health"))
        .respond_with(ResponseTemplate::new(200).set_body_json(&body))
        .expect(1)
        .mount(&server)
        .await;

    let resp = client.health().await.unwrap();
    assert_eq!(resp.status, "healthy");
    assert_eq!(resp.db.unwrap(), "connected");
}

#[tokio::test]
async fn test_401_error() {
    let (server, client) = setup_mock().await;

    Mock::given(method("GET"))
        .and(path("/v1/scores/GA123"))
        .respond_with(ResponseTemplate::new(401).set_body_string("Unauthorized"))
        .expect(1)
        .mount(&server)
        .await;

    let err = client.get_score("GA123").await.unwrap_err();
    match err {
        ledgerlens_sdk::LedgerLensError::Unauthorized(_) => {}
        _ => panic!("Expected Unauthorized error, got {:?}", err),
    }
}

#[tokio::test]
async fn test_404_error() {
    let (server, client) = setup_mock().await;

    Mock::given(method("GET"))
        .and(path("/v1/scores/UNKNOWN"))
        .respond_with(ResponseTemplate::new(404).set_body_string("Not found"))
        .expect(1)
        .mount(&server)
        .await;

    let err = client.get_score("UNKNOWN").await.unwrap_err();
    match err {
        ledgerlens_sdk::LedgerLensError::NotFound(_) => {}
        _ => panic!("Expected NotFound error, got {:?}", err),
    }
}

#[tokio::test]
async fn test_429_error() {
    let (server, client) = setup_mock().await;

    Mock::given(method("GET"))
        .and(path("/v1/scores/GA123"))
        .respond_with(ResponseTemplate::new(429).set_body_string("Rate limited"))
        .expect(1)
        .mount(&server)
        .await;

    let err = client.get_score("GA123").await.unwrap_err();
    match err {
        ledgerlens_sdk::LedgerLensError::RateLimited(_) => {}
        _ => panic!("Expected RateLimited error, got {:?}", err),
    }
}

#[tokio::test]
async fn test_debug_redacts_api_key() {
    let client = LedgerLensClient::new("http://localhost", Some("sk_secret123".into()));
    let debug_str = format!("{:?}", client);
    assert!(!debug_str.contains("sk_secret123"), "API key leaked in Debug output");
    assert!(debug_str.contains("***"), "Debug should show redacted key");
}