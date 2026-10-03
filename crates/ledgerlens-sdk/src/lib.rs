//! # LedgerLens SDK (Rust)
//!
//! A typed Rust client for the LedgerLens REST API, with optional zero-knowledge
//! proof verification for threshold proofs.
//!
//! ## Quick Start
//!
//! ```no_run
//! use ledgerlens_sdk::LedgerLensClient;
//!
//! #[tokio::main]
//! async fn main() -> Result<(), Box<dyn std::error::Error>> {
//!     let client = LedgerLensClient::new(
//!         "https://api.ledgerlens.io",
//!         Some("sk_your_api_key".into()),
//!     );
//!
//!     // Fetch scores for a specific wallet
//!     let scores = client.get_score("GA…").await?;
//!     println!("{:?}", scores);
//!
//!     // Check health
//!     let health = client.health().await?;
//!     println!("API status: {}", health.status);
//!
//!     Ok(())
//! }
//! ```
//!
//! ## Feature Flags
//!
//! - `std` (default): HTTP client and `std::error::Error` impls.
//! - `async` (default): Enable async/await support via `tokio` (implies `std`).
//! - `zk-verify`: Enable ZK threshold proof verification using ark-bn254.
//!
//! Build with `default-features = false` for a `no_std` + `alloc` target such
//! as `wasm32-unknown-unknown`; only models, errors and (with `zk-verify`)
//! proof verification are available there.

#![cfg_attr(not(feature = "std"), no_std)]

extern crate alloc;

#[cfg(feature = "std")]
pub mod client;
pub mod error;
pub mod models;

#[cfg(feature = "zk-verify")]
pub mod zk;

// Re-exports for convenience.
#[cfg(feature = "std")]
pub use client::LedgerLensClient;
pub use error::LedgerLensError;
pub use models::{CrossChainLink, HealthStatus, Ring, RiskScore, WalletScoresResponse};

#[cfg(feature = "zk-verify")]
pub use zk::verify_threshold_proof;

#[cfg(feature = "zk-verify")]
pub use error::ZkVerifyError;

#[cfg(feature = "zk-verify")]
pub use models::{BitProof, ThresholdProof};
