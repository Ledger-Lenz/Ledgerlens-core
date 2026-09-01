use std::fmt;

use crate::error::LedgerLensError;
use crate::models::{HealthStatus, Ring, RiskScore, WalletScoresResponse};

/// A typed HTTP client for the LedgerLens REST API.
///
/// # Security
///
/// - TLS verification is enabled by default. Use
///   [`danger_accept_invalid_certs`](Self::danger_accept_invalid_certs) only for
///   local testing against a self-signed server.
/// - The `api_key` is redacted from `Debug` output to prevent accidental logging.
///
/// # Example (async)
///
/// ```no_run
/// use ledgerlens_sdk::LedgerLensClient;
///
/// #[tokio::main]
/// async fn main() -> Result<(), Box<dyn std::error::Error>> {
///     let client = LedgerLensClient::new("https://api.ledgerlens.io", Some("sk_...".into()));
///     let scores = client.get_scores(None).await?;
///     println!("Got {} scores", scores.len());
///     Ok(())
/// }
/// ```
pub struct LedgerLensClient {
    base_url: String,
    api_key: Option<String>,
    http: reqwest::Client,
}

// Manual Debug impl to redact the API key.
impl fmt::Debug for LedgerLensClient {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("LedgerLensClient")
            .field("base_url", &self.base_url)
            .field("api_key", &self.api_key.as_deref().map(|_| "***"))
            .field("http", &self.http)
            .finish()
    }
}

impl LedgerLensClient {
    /// Build the internal `reqwest::Client`, panicking only if the underlying
    /// TLS backend cannot be initialized (which indicates a bug, not user error).
    fn build_http_client(danger_accept_invalid_certs: bool) -> reqwest::Client {
        reqwest::Client::builder()
            .user_agent("ledgerlens-sdk/0.1.0")
            .danger_accept_invalid_certs(danger_accept_invalid_certs)
            .build()
            .expect("Failed to build reqwest Client; this is a bug")
    }

    /// Create a new client pointing at the given `base_url`.
    ///
    /// TLS verification is enabled by default. Pass `None` for `api_key` if the
    /// server does not require authentication (e.g. a local dev instance).
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use ledgerlens_sdk::LedgerLensClient;
    ///
    /// // With an API key
    /// let client = LedgerLensClient::new(
    ///     "https://api.ledgerlens.io",
    ///     Some("sk_your_api_key".into()),
    /// );
    ///
    /// // Without an API key (local dev server)
    /// let dev_client = LedgerLensClient::new("http://localhost:8000", None);
    /// ```
    pub fn new(base_url: impl Into<String>, api_key: Option<String>) -> Self {
        Self {
            base_url: base_url.into(),
            api_key,
            http: Self::build_http_client(false),
        }
    }

    /// Create a new client that accepts invalid TLS certificates.
    ///
    /// # Warning
    ///
    /// This disables TLS certificate verification. Only use this for local
    /// testing against a server with a self-signed certificate. Never use this
    /// in production.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use ledgerlens_sdk::LedgerLensClient;
    ///
    /// // Only for local development with self-signed certs — never use in production.
    /// let client = LedgerLensClient::danger_accept_invalid_certs(
    ///     "https://localhost:8443",
    ///     None,
    /// );
    /// ```
    pub fn danger_accept_invalid_certs(
        base_url: impl Into<String>,
        api_key: Option<String>,
    ) -> Self {
        Self {
            base_url: base_url.into(),
            api_key,
            http: Self::build_http_client(true),
        }
    }

    /// Build the shared internal `reqwest::Client`, optionally accepting
    /// invalid TLS certificates.
    fn build_http_client(danger_accept_invalid_certs: bool) -> reqwest::Client {
        reqwest::Client::builder()
            .user_agent("ledgerlens-sdk/0.1.0")
            .danger_accept_invalid_certs(danger_accept_invalid_certs)
            .build()
            .expect("Failed to build reqwest Client; this is a bug")
    }

    /// Build a full URL from a path segment.
    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url.trim_end_matches('/'), path)
    }

    /// Attach the API key header if one is configured.
    fn auth_header(&self) -> Option<(&str, &str)> {
        self.api_key
            .as_ref()
            .map(|k| ("X-LedgerLens-API-Key", k.as_str()))
    }

    /// Perform a GET request and deserialize the response.
    async fn get_json<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
    ) -> Result<T, LedgerLensError> {
        let url = self.url(path);
        let mut req = self.http.get(&url);

        if let Some((key, val)) = self.auth_header() {
            req = req.header(key, val);
        }

        let resp = req.send().await?;

        if !resp.status().is_success() {
            let status = resp.status().as_u16();
            let body = resp.text().await.unwrap_or_default();
            return Err(match status {
                401 => LedgerLensError::Unauthorized(body),
                404 => LedgerLensError::NotFound(body),
                429 => LedgerLensError::RateLimited(body),
                _ => LedgerLensError::Api {
                    status_code: status,
                    message: body,
                },
            });
        }

        let text = resp.text().await?;
        let value: T = serde_json::from_str(&text)?;
        Ok(value)
    }

    // ------------------------------------------------------------------
    // Public API methods
    // ------------------------------------------------------------------

    /// Fetch the latest score for a specific wallet across all asset pairs.
    ///
    /// `GET /v1/scores/{wallet}`
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use ledgerlens_sdk::LedgerLensClient;
    ///
    /// #[tokio::main]
    /// async fn main() -> Result<(), Box<dyn std::error::Error>> {
    ///     let client = LedgerLensClient::new("https://api.ledgerlens.io", None);
    ///     let response = client.get_score("GABCDEFGHIJKLMNOPQRSTUVWXYZ012345678901234567890123456").await?;
    ///     for score in &response.scores {
    ///         println!("Pair: {}, Score: {}", score.asset_pair, score.score);
    ///     }
    ///     Ok(())
    /// }
    /// ```
    pub async fn get_score(&self, wallet: &str) -> Result<WalletScoresResponse, LedgerLensError> {
        self.get_json(&format!("/v1/scores/{}", wallet)).await
    }

    /// Fetch all scores, optionally filtered by asset pair.
    ///
    /// `GET /v1/scores`
    ///
    /// Pass `None` to retrieve scores for all asset pairs, or `Some(pair)` to
    /// filter to a single asset pair (e.g. `"XLM/USDC"`).
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use ledgerlens_sdk::LedgerLensClient;
    ///
    /// #[tokio::main]
    /// async fn main() -> Result<(), Box<dyn std::error::Error>> {
    ///     let client = LedgerLensClient::new("https://api.ledgerlens.io", None);
    ///
    ///     // All scores across all asset pairs
    ///     let all_scores = client.get_scores(None).await?;
    ///     println!("Total scores: {}", all_scores.len());
    ///
    ///     // Scores filtered to the XLM/USDC pair only
    ///     let xlm_usdc = client.get_scores(Some("XLM/USDC")).await?;
    ///     println!("XLM/USDC scores: {}", xlm_usdc.len());
    ///
    ///     Ok(())
    /// }
    /// ```
    pub async fn get_scores(
        &self,
        asset_pair: Option<&str>,
    ) -> Result<Vec<RiskScore>, LedgerLensError> {
        let path = match asset_pair {
            Some(pair) => format!("/v1/scores?asset_pair={}", urlencoding(pair)),
            None => "/v1/scores".to_string(),
        };
        self.get_json(&path).await
    }

    /// Fetch detected wash-trading rings.
    ///
    /// `GET /v1/rings`
    ///
    /// Returns all rings currently detected by the pipeline. Each ring includes
    /// the participant wallet addresses, total volume, and detection timestamp.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use ledgerlens_sdk::LedgerLensClient;
    ///
    /// #[tokio::main]
    /// async fn main() -> Result<(), Box<dyn std::error::Error>> {
    ///     let client = LedgerLensClient::new("https://api.ledgerlens.io", None);
    ///     let rings = client.get_rings().await?;
    ///     for ring in &rings {
    ///         println!(
    ///             "Ring {} — {} wallets, volume: {}",
    ///             ring.ring_id,
    ///             ring.wallets.len(),
    ///             ring.total_volume
    ///         );
    ///     }
    ///     Ok(())
    /// }
    /// ```
    pub async fn get_rings(&self) -> Result<Vec<Ring>, LedgerLensError> {
        self.get_json("/v1/rings").await
    }

    /// Check the API health.
    ///
    /// `GET /health`
    ///
    /// Returns the overall status of the API, including database connectivity
    /// and model loading status.
    ///
    /// # Examples
    ///
    /// ```no_run
    /// use ledgerlens_sdk::LedgerLensClient;
    ///
    /// #[tokio::main]
    /// async fn main() -> Result<(), Box<dyn std::error::Error>> {
    ///     let client = LedgerLensClient::new("https://api.ledgerlens.io", None);
    ///     let health = client.health().await?;
    ///     if health.status == "ok" {
    ///         println!("API is healthy");
    ///     } else {
    ///         eprintln!("API degraded: {:?}", health.db);
    ///     }
    ///     Ok(())
    /// }
    /// ```
    pub async fn health(&self) -> Result<HealthStatus, LedgerLensError> {
        self.get_json("/health").await
    }
}

/// Simple URL-encoding for query parameters (only encodes spaces for now).
fn urlencoding(s: &str) -> String {
    s.replace(' ', "%20")
}
