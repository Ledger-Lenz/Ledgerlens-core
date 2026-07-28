use chrono::{DateTime, Utc};
use serde::Deserialize;

/// A single wallet/asset-pair risk score, as returned by the scoring pipeline.
///
/// Field names and types match the Python SDK (`packages/ledgerlens-sdk/src/ledgerlens/models.py`)
/// and the TypeScript SDK (`sdk/`) exactly.
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct RiskScore {
    /// Stellar wallet address (G...).
    pub wallet: String,
    /// Asset pair, e.g. "XLM/USDC".
    pub asset_pair: String,
    /// 0-100; higher = more suspicious.
    pub score: u8,
    /// Whether the Benford analysis flagged this wallet.
    pub benford_flag: bool,
    /// Whether the ML model flagged this wallet.
    pub ml_flag: bool,
    /// Confidence in the score (0-100).
    pub confidence: u8,
    /// Whether the score has been disputed.
    #[serde(default)]
    pub disputed: bool,
    /// When the score was computed.
    pub timestamp: DateTime<Utc>,
    /// Lower bound of 90% conformal prediction interval (optional, v2+).
    pub score_lower: Option<f64>,
    /// Upper bound of 90% conformal prediction interval (optional, v2+).
    pub score_upper: Option<f64>,
    /// Class indices in the conformal prediction set (optional, v2+).
    pub prediction_set: Option<Vec<u8>>,
    /// Target coverage level (1 - alpha) of the prediction set (optional, v2+).
    pub coverage_guarantee: Option<f64>,
}

/// A cross-chain link discovered via bridge transfer analysis.
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct CrossChainLink {
    /// The chain identifier (e.g. "ethereum", "polygon").
    pub chain: String,
    /// The EVM wallet address on the other chain.
    pub evm_wallet: String,
    /// ISO-8601 timestamp of the most recent bridge transfer.
    pub last_bridge_at: String,
}

/// Response shape of `GET /v1/scores/{wallet}`.
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct WalletScoresResponse {
    /// Risk scores for the wallet across all asset pairs.
    pub scores: Vec<RiskScore>,
    /// Cross-chain links discovered via bridge transfers (may be empty).
    #[serde(default)]
    pub cross_chain_links: Vec<CrossChainLink>,
}

/// A detected wash-trading ring.
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct Ring {
    /// Unique identifier for the ring.
    pub ring_id: String,
    /// The asset pair the ring was detected on.
    pub asset_pair: String,
    /// Wallet addresses in the ring.
    pub wallets: Vec<String>,
    /// Total volume traded within the ring.
    pub total_volume: f64,
    /// Number of trades within the ring.
    pub trade_count: u64,
    /// When the ring was detected.
    pub detected_at: String,
}

/// Response shape of `GET /health`.
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct HealthStatus {
    /// Overall health status (e.g. "healthy", "degraded").
    pub status: String,
    /// Database connectivity status.
    pub db: Option<String>,
    /// Model loading status.
    pub models: Option<String>,
    /// Circuit breaker statuses.
    pub circuits: Option<std::collections::HashMap<String, String>>,
}

/// A ZK threshold proof for verifying that a committed score is >= a threshold
/// without revealing the exact score.
///
/// This mirrors the proof dict format from `detection/zk_prover.py`.
#[cfg(feature = "zk-verify")]
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct ThresholdProof {
    /// X coordinate of the Pedersen commitment point.
    pub score_commit_x: String,
    /// Y coordinate of the Pedersen commitment point.
    pub score_commit_y: String,
    /// Per-bit commitment and response data.
    pub bits: Vec<BitProof>,
}

/// A single bit proof within a threshold proof.
#[cfg(feature = "zk-verify")]
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub struct BitProof {
    /// X coordinate of the bit commitment point.
    pub commit_x: String,
    /// Y coordinate of the bit commitment point.
    pub commit_y: String,
    /// Challenge for the "bit is 0" statement.
    pub c0: String,
    /// Challenge for the "bit is 1" statement.
    pub c1: String,
    /// Response for the "bit is 0" statement.
    pub s0: String,
    /// Response for the "bit is 1" statement.
    pub s1: String,
}