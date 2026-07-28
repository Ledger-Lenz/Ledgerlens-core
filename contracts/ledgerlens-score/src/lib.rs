//! LedgerLens Score — Soroban contract
//!
//! Stores and validates wallet risk scores produced by the off-chain
//! LedgerLens pipeline, and enforces privileged parameter governance with
//! **structured authorization-failure telemetry** that does not disclose
//! signer-set internals.
//!
//! # Module structure
//!
//! ```text
//! lib.rs                  — public contract surface (this file)
//! parameter_governance.rs — privileged parameter writes and role checks
//! events.rs               — typed event emitters (locked schema)
//! errors.rs               — contracterror enum (stable numeric codes)
//! ```
//!
//! # Authorization model
//!
//! Two privileged roles exist:
//!
//! | Role     | Capabilities                                            |
//! |----------|---------------------------------------------------------|
//! | admin    | lock/unlock, transfer admin, reassign governor           |
//! | governor | update score-max, score-floor parameters                |
//!
//! Both roles use Soroban's `require_auth()` for cryptographic enforcement
//! followed by an address-equality check against the stored role address.
//! On failure, an `auth_denied` event is emitted **before** the function
//! returns an error — this ensures the event appears in the transaction's
//! diagnostic stream even when the host reverts.
//!
//! # Telemetry — no signer-set disclosure
//!
//! `auth_denied` events carry:
//! - `reason` — one of `not_admin`, `not_gov`, `locked`, `no_quorum`
//! - `caller` — the address the caller supplied (already public)
//!
//! They never carry: threshold values, signer-set size, internal role
//! addresses, or any indication of how "close" the caller was to success.
//!
//! # Score writes (oracle path)
//!
//! `submit_score` is intentionally *not* gated by a role check; it expects
//! to be called by the `oracle_aggregator` contract after quorum has already
//! been verified there.  A future version may add cross-contract auth here.

#![no_std]

use soroban_sdk::{contract, contractimpl, Address, Env, symbol_short};

mod errors;
mod events;
pub mod parameter_governance;

use errors::LedgerLensError;
use events::{emit_score_updated, AuthDeniedReason, emit_auth_denied};

// ---------------------------------------------------------------------------
// Storage keys (score store)
// ---------------------------------------------------------------------------

use soroban_sdk::{Map, Symbol};

const KEY_SCORES: Symbol = symbol_short!("SCORES");

mod tests;

// ---------------------------------------------------------------------------
// Contract
// ---------------------------------------------------------------------------

#[contract]
pub struct LedgerLensScore;

#[contractimpl]
impl LedgerLensScore {
    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    /// Initialise the contract with admin and governor roles.
    ///
    /// Returns [`LedgerLensError::AlreadyInitialized`] if called more than
    /// once.  This is the only way to set the admin — there is no back-door.
    pub fn initialize(
        env: Env,
        admin: Address,
        governor: Address,
    ) -> Result<(), LedgerLensError> {
        parameter_governance::initialize(&env, admin, governor)
    }

    // ------------------------------------------------------------------
    // Score writes (oracle-path, not role-gated at this layer)
    // ------------------------------------------------------------------

    /// Record a risk score for `wallet`.
    ///
    /// This entry point is designed to be called by the oracle_aggregator
    /// after it has verified quorum.  Score validation (range check against
    /// current `score_max`) is enforced here; the contract does not need to
    /// trust the caller's arithmetic.
    ///
    /// Returns [`LedgerLensError::ContractLocked`] when the contract is
    /// administratively locked, and [`LedgerLensError::ScoreOutOfRange`]
    /// when `score > score_max`.
    ///
    /// # Events
    ///
    /// On success: `score_updated { wallet, score, ledger }`
    /// On lock denial: `auth_denied { reason: locked, caller: wallet }`
    pub fn submit_score(
        env: Env,
        wallet: Address,
        score: u32,
    ) -> Result<(), LedgerLensError> {
        // Gate on lock state — emit a telemetry event so operators see
        // rejected submissions.  We use the wallet as the "caller" because
        // the submission is on behalf of that wallet.
        if parameter_governance::is_locked(&env) {
            emit_auth_denied(&env, AuthDeniedReason::ContractLocked, &wallet);
            return Err(LedgerLensError::ContractLocked);
        }

        let score_max = parameter_governance::get_score_max(&env);
        if score > score_max {
            return Err(LedgerLensError::ScoreOutOfRange);
        }

        let mut scores: Map<Address, u32> =
            env.storage().instance().get(&KEY_SCORES).unwrap_or(Map::new(&env));
        scores.set(wallet.clone(), score);
        env.storage().instance().set(&KEY_SCORES, &scores);

        emit_score_updated(&env, &wallet, score);
        Ok(())
    }

    // ------------------------------------------------------------------
    // Score reads
    // ------------------------------------------------------------------

    /// Return the stored risk score for `wallet`, or `None` if not recorded.
    pub fn get_score(env: Env, wallet: Address) -> Option<u32> {
        let scores: Map<Address, u32> =
            env.storage().instance().get(&KEY_SCORES).unwrap_or(Map::new(&env));
        scores.get(wallet)
    }

    // ------------------------------------------------------------------
    // Governance — admin operations (delegated to parameter_governance)
    // ------------------------------------------------------------------

    /// Transfer admin control.  Requires current admin signature.
    pub fn set_admin(
        env: Env,
        caller: Address,
        new_admin: Address,
    ) -> Result<(), LedgerLensError> {
        parameter_governance::set_admin(&env, caller, new_admin)
    }

    /// Lock the contract — prevents score submissions and parameter updates.
    pub fn lock(env: Env, caller: Address) -> Result<(), LedgerLensError> {
        parameter_governance::lock(&env, caller)
    }

    /// Unlock the contract.
    pub fn unlock(env: Env, caller: Address) -> Result<(), LedgerLensError> {
        parameter_governance::unlock(&env, caller)
    }

    /// Reassign the governor role.  Requires admin signature.
    pub fn set_governor(
        env: Env,
        caller: Address,
        new_governor: Address,
    ) -> Result<(), LedgerLensError> {
        parameter_governance::set_governor(&env, caller, new_governor)
    }

    // ------------------------------------------------------------------
    // Governance — governor operations
    // ------------------------------------------------------------------

    /// Update the maximum accepted score value.  Requires governor signature.
    pub fn set_score_max(
        env: Env,
        caller: Address,
        new_max: u32,
    ) -> Result<(), LedgerLensError> {
        parameter_governance::set_score_max(&env, caller, new_max)
    }

    /// Update the score floor.  Requires governor signature.
    pub fn set_score_floor(
        env: Env,
        caller: Address,
        new_floor: u32,
    ) -> Result<(), LedgerLensError> {
        parameter_governance::set_score_floor(&env, caller, new_floor)
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    /// Return whether the contract is locked.
    pub fn is_locked(env: Env) -> bool {
        parameter_governance::is_locked(&env)
    }

    /// Return the current score-max parameter.
    pub fn get_score_max(env: Env) -> u32 {
        parameter_governance::get_score_max(&env)
    }

    /// Return the current score-floor parameter.
    pub fn get_score_floor(env: Env) -> u32 {
        parameter_governance::get_score_floor(&env)
    }

    /// Return the parameter-schema version (bumped on every governor write).
    pub fn get_param_version(env: Env) -> u32 {
        parameter_governance::get_param_version(&env)
    }
}
