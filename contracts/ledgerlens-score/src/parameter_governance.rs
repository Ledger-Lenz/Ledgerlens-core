//! Parameter governance for the LedgerLens risk-score contract.
//!
//! This module owns all *privileged configuration writes* — the set of
//! operations that change how scores are accepted, validated, or bounded.
//! Every function in this module emits a structured event on **both** success
//! and failure so that operators and alerting systems have a deterministic,
//! auditable record without needing to inspect raw host-function traces.
//!
//! # Roles
//!
//! ```text
//! admin    — bootstrapped at initialisation; can update the admin address
//!            and lock/unlock the contract.
//! governor — higher-trust role; can change risk-model parameters and the
//!            score-floor/ceiling bounds.
//! ```
//!
//! Role separation means an operator can grant narrow write access (governor)
//! without granting the ability to transfer admin control.
//!
//! # Signer-strategy opacity
//!
//! No function in this module exposes *how many* addresses are enrolled in a
//! role, nor whether a given address is in the authorised set.  Failure events
//! carry only the *caller* address (already public) and a functional reason
//! code.  This prevents adversarial callers from using telemetry to probe
//! signer-set membership or threshold values.
//!
//! # Storage layout
//!
//! All keys are `Symbol` stored in instance storage (bounded by contract
//! lifetime; no persistent TTL management required at this layer).
//!
//! | Key            | Type    | Purpose                                    |
//! |----------------|---------|--------------------------------------------|
//! | `ADMIN`        | Address | Current admin address                      |
//! | `GOVERNOR`     | Address | Current governor address                   |
//! | `LOCKED`       | bool    | When true, all privileged writes are gated |
//! | `SCORE_MAX`    | u32     | Maximum accepted score value (≤ 100)       |
//! | `SCORE_FLOOR`  | u32     | Minimum score treated as non-zero risk     |
//! | `PARAM_VERSION`| u32     | Monotonically increasing schema version    |

use soroban_sdk::{Address, Env, Symbol, symbol_short};

use crate::errors::LedgerLensError;
use crate::events::{emit_auth_denied, emit_param_updated, AuthDeniedReason};

// ---------------------------------------------------------------------------
// Storage key constants
// ---------------------------------------------------------------------------

pub(crate) const KEY_ADMIN: Symbol        = symbol_short!("ADMIN");
pub(crate) const KEY_GOVERNOR: Symbol     = symbol_short!("GOVERNOR");
pub(crate) const KEY_LOCKED: Symbol       = symbol_short!("LOCKED");
pub(crate) const KEY_SCORE_MAX: Symbol    = symbol_short!("SCORE_MAX");
pub(crate) const KEY_SCORE_FLOOR: Symbol  = symbol_short!("S_FLOOR");
pub(crate) const KEY_PARAM_VER: Symbol    = symbol_short!("PARAM_VER");

/// Hard upper bound on any score value — enforced at the contract layer so
/// off-chain systems can assume scores are always in [0, SCORE_HARD_MAX].
pub(crate) const SCORE_HARD_MAX: u32 = 100;

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

/// Initialise governance roles and default parameters.
///
/// Must be called exactly once.  Subsequent calls return
/// [`LedgerLensError::AlreadyInitialized`].
///
/// # Default parameters
///
/// | Parameter   | Default |
/// |-------------|---------|
/// | `score_max` | 100     |
/// | `score_floor` | 0     |
/// | `locked`    | false   |
pub fn initialize(
    env: &Env,
    admin: Address,
    governor: Address,
) -> Result<(), LedgerLensError> {
    if env.storage().instance().has(&KEY_ADMIN) {
        // Do not emit an auth event here — double-init is a configuration
        // error, not an authorisation failure.
        return Err(LedgerLensError::AlreadyInitialized);
    }

    env.storage().instance().set(&KEY_ADMIN, &admin);
    env.storage().instance().set(&KEY_GOVERNOR, &governor);
    env.storage().instance().set(&KEY_LOCKED, &false);
    env.storage().instance().set(&KEY_SCORE_MAX, &SCORE_HARD_MAX);
    env.storage().instance().set(&KEY_SCORE_FLOOR, &0u32);
    env.storage().instance().set(&KEY_PARAM_VER, &0u32);

    Ok(())
}

// ---------------------------------------------------------------------------
// Role checks (internal helpers)
// ---------------------------------------------------------------------------

/// Require that `caller` is the current admin.
///
/// Emits `auth_denied / not_admin` and returns an error when the check fails.
/// Does **not** reveal which address is the current admin.
fn require_admin(env: &Env, caller: &Address) -> Result<(), LedgerLensError> {
    // require_auth() traps when the invoker's auth context does not satisfy
    // the address.  We call it first so the host enforces the cryptographic
    // proof before we compare addresses — this prevents a time-of-check /
    // time-of-use gap where someone passes the address comparison without
    // having a valid signature.
    caller.require_auth();

    let admin: Address = env
        .storage()
        .instance()
        .get(&KEY_ADMIN)
        .unwrap_or_else(|| panic!("contract not initialized"));

    if &admin != caller {
        emit_auth_denied(env, AuthDeniedReason::NotAdmin, caller);
        return Err(LedgerLensError::NotAdmin);
    }
    Ok(())
}

/// Require that `caller` is the current governor.
///
/// Emits `auth_denied / not_gov` and returns an error when the check fails.
fn require_governor(env: &Env, caller: &Address) -> Result<(), LedgerLensError> {
    caller.require_auth();

    let governor: Address = env
        .storage()
        .instance()
        .get(&KEY_GOVERNOR)
        .unwrap_or_else(|| panic!("contract not initialized"));

    if &governor != caller {
        emit_auth_denied(env, AuthDeniedReason::NotGovernor, caller);
        return Err(LedgerLensError::NotGovernor);
    }
    Ok(())
}

/// Require that the contract is **not** locked.
///
/// Called *after* role checks so that the audit trail shows the authenticated
/// caller, not an anonymous "locked" rejection.
fn require_unlocked(env: &Env, caller: &Address) -> Result<(), LedgerLensError> {
    let locked: bool = env
        .storage()
        .instance()
        .get(&KEY_LOCKED)
        .unwrap_or(false);

    if locked {
        emit_auth_denied(env, AuthDeniedReason::ContractLocked, caller);
        return Err(LedgerLensError::ContractLocked);
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Admin operations
// ---------------------------------------------------------------------------

/// Transfer admin control to `new_admin`.
///
/// Requires the current admin's signature.  Fails with
/// [`LedgerLensError::NotAdmin`] (plus an `auth_denied` event) if the
/// caller is not the current admin.
pub fn set_admin(
    env: &Env,
    caller: Address,
    new_admin: Address,
) -> Result<(), LedgerLensError> {
    require_admin(env, &caller)?;
    // No unlocked check — admin transfer is allowed even while locked so the
    // operator can recover a misconfigured lock state.
    env.storage().instance().set(&KEY_ADMIN, &new_admin);
    emit_param_updated(env, symbol_short!("ADMIN"), 0);
    Ok(())
}

/// Lock the contract, preventing all governor-level parameter writes.
///
/// The admin can still call `set_admin` and `unlock` while locked.
pub fn lock(env: &Env, caller: Address) -> Result<(), LedgerLensError> {
    require_admin(env, &caller)?;
    env.storage().instance().set(&KEY_LOCKED, &true);
    emit_param_updated(env, symbol_short!("LOCKED"), 1);
    Ok(())
}

/// Unlock the contract.
pub fn unlock(env: &Env, caller: Address) -> Result<(), LedgerLensError> {
    require_admin(env, &caller)?;
    env.storage().instance().set(&KEY_LOCKED, &false);
    emit_param_updated(env, symbol_short!("LOCKED"), 0);
    Ok(())
}

/// Transfer governor control to `new_governor`.
///
/// Requires admin privileges (only the admin can reassign the governor role).
pub fn set_governor(
    env: &Env,
    caller: Address,
    new_governor: Address,
) -> Result<(), LedgerLensError> {
    require_admin(env, &caller)?;
    require_unlocked(env, &caller)?;
    env.storage().instance().set(&KEY_GOVERNOR, &new_governor);
    emit_param_updated(env, symbol_short!("GOVERNOR"), 0);
    Ok(())
}

// ---------------------------------------------------------------------------
// Governor operations
// ---------------------------------------------------------------------------

/// Set the maximum accepted score.
///
/// `new_max` must be in `[1, SCORE_HARD_MAX]`.  Requires governor auth and
/// an unlocked contract.
pub fn set_score_max(
    env: &Env,
    caller: Address,
    new_max: u32,
) -> Result<(), LedgerLensError> {
    require_governor(env, &caller)?;
    require_unlocked(env, &caller)?;

    if new_max == 0 || new_max > SCORE_HARD_MAX {
        return Err(LedgerLensError::InvalidParameter);
    }

    env.storage().instance().set(&KEY_SCORE_MAX, &new_max);
    bump_param_version(env);
    emit_param_updated(env, KEY_SCORE_MAX, new_max);
    Ok(())
}

/// Set the score floor — scores at or below this value are treated as
/// "no meaningful risk signal" by downstream consumers.
///
/// `new_floor` must be strictly less than the current `score_max`.
pub fn set_score_floor(
    env: &Env,
    caller: Address,
    new_floor: u32,
) -> Result<(), LedgerLensError> {
    require_governor(env, &caller)?;
    require_unlocked(env, &caller)?;

    let score_max: u32 = env
        .storage()
        .instance()
        .get(&KEY_SCORE_MAX)
        .unwrap_or(SCORE_HARD_MAX);

    if new_floor >= score_max {
        return Err(LedgerLensError::InvalidParameter);
    }

    env.storage().instance().set(&KEY_SCORE_FLOOR, &new_floor);
    bump_param_version(env);
    emit_param_updated(env, KEY_SCORE_FLOOR, new_floor);
    Ok(())
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

fn bump_param_version(env: &Env) {
    let v: u32 = env
        .storage()
        .instance()
        .get(&KEY_PARAM_VER)
        .unwrap_or(0);
    env.storage().instance().set(&KEY_PARAM_VER, &v.saturating_add(1));
}

// ---------------------------------------------------------------------------
// View helpers (used by lib.rs)
// ---------------------------------------------------------------------------

/// Return the current admin address.  Does not require authentication.
pub fn get_admin(env: &Env) -> Option<Address> {
    env.storage().instance().get(&KEY_ADMIN)
}

/// Return whether the contract is currently locked.
pub fn is_locked(env: &Env) -> bool {
    env.storage().instance().get(&KEY_LOCKED).unwrap_or(false)
}

/// Return the current score-max parameter.
pub fn get_score_max(env: &Env) -> u32 {
    env.storage().instance().get(&KEY_SCORE_MAX).unwrap_or(SCORE_HARD_MAX)
}

/// Return the current score-floor parameter.
pub fn get_score_floor(env: &Env) -> u32 {
    env.storage().instance().get(&KEY_SCORE_FLOOR).unwrap_or(0)
}

/// Return the current parameter-schema version.
pub fn get_param_version(env: &Env) -> u32 {
    env.storage().instance().get(&KEY_PARAM_VER).unwrap_or(0)
}
