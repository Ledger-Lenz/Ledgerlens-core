use std::fmt;

/// Errors that can occur when using the LedgerLens client.
#[derive(Debug, Clone)]
pub enum LedgerLensError {
    /// HTTP request failed (network error, DNS resolution failure, etc.)
    HttpError(String),
    /// The API returned an error response.
    Api {
        status_code: u16,
        message: String,
    },
    /// The request was unauthorized (401).
    Unauthorized(String),
    /// The requested resource was not found (404).
    NotFound(String),
    /// Rate limit exceeded (429).
    RateLimited(String),
    /// JSON deserialization failed.
    Deserialization(String),
    /// URL parsing failed.
    InvalidUrl(String),
    /// TLS certificate validation failed; use `danger_accept_invalid_certs(true)`
    /// only for local testing.
    TlsError(String),
}

impl std::error::Error for LedgerLensError {}

impl fmt::Display for LedgerLensError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LedgerLensError::HttpError(msg) => write!(f, "HTTP error: {}", msg),
            LedgerLensError::Api { status_code, message } => {
                write!(f, "API error ({}): {}", status_code, message)
            }
            LedgerLensError::Unauthorized(msg) => write!(f, "Unauthorized (401): {}", msg),
            LedgerLensError::NotFound(msg) => write!(f, "Not found (404): {}", msg),
            LedgerLensError::RateLimited(msg) => write!(f, "Rate limited (429): {}", msg),
            LedgerLensError::Deserialization(msg) => write!(f, "Deserialization error: {}", msg),
            LedgerLensError::InvalidUrl(msg) => write!(f, "Invalid URL: {}", msg),
            LedgerLensError::TlsError(msg) => write!(f, "TLS error: {}", msg),
        }
    }
}

impl From<reqwest::Error> for LedgerLensError {
    fn from(e: reqwest::Error) -> Self {
        if e.is_status() {
            let status = e.status().unwrap_or_default();
            let msg = e.to_string();
            match status.as_u16() {
                401 => LedgerLensError::Unauthorized(msg),
                404 => LedgerLensError::NotFound(msg),
                429 => LedgerLensError::RateLimited(msg),
                _ => LedgerLensError::Api {
                    status_code: status.as_u16(),
                    message: msg,
                },
            }
        } else if e.is_connect() || e.is_timeout() {
            LedgerLensError::HttpError(e.to_string())
        } else if e.is_builder() {
            LedgerLensError::InvalidUrl(e.to_string())
        } else {
            LedgerLensError::HttpError(e.to_string())
        }
    }
}

impl From<serde_json::Error> for LedgerLensError {
    fn from(e: serde_json::Error) -> Self {
        LedgerLensError::Deserialization(e.to_string())
    }
}

/// Errors from ZK proof verification (only available with `zk-verify` feature).
#[cfg(feature = "zk-verify")]
#[derive(Debug, Clone)]
pub enum ZkVerifyError {
    /// The proof has an invalid wire format or field count.
    InvalidFormat(String),
    /// A curve arithmetic operation failed (point not on curve, etc.).
    CurveError(String),
    /// The Fiat-Shamir challenge mismatched.
    ChallengeMismatch,
    /// A bit commitment verification failed.
    BitProofFailed(usize),
    /// The aggregate sum of bit commitments does not match the expected value.
    AggregateMismatch,
    /// The threshold exceeds MAX_SCORE (100).
    InvalidThreshold(u32),
}

#[cfg(feature = "zk-verify")]
impl std::error::Error for ZkVerifyError {}

#[cfg(feature = "zk-verify")]
impl fmt::Display for ZkVerifyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ZkVerifyError::InvalidFormat(msg) => write!(f, "Invalid proof format: {}", msg),
            ZkVerifyError::CurveError(msg) => write!(f, "Curve arithmetic error: {}", msg),
            ZkVerifyError::ChallengeMismatch => write!(f, "Fiat-Shamir challenge mismatch"),
            ZkVerifyError::BitProofFailed(i) => write!(f, "Bit proof {} failed verification", i),
            ZkVerifyError::AggregateMismatch => {
                write!(f, "Aggregate sum of bit commitments does not match")
            }
            ZkVerifyError::InvalidThreshold(t) => {
                write!(f, "Invalid threshold {} (must be <= 100)", t)
            }
        }
    }
}