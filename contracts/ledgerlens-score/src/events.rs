//! Structured telemetry events for the ledgerlens-score contract.
//!
//! # Security design — no signer-set disclosure
//!
//! Every `AuthDenied` event carries an `AuthDeniedReason` that tells
//! operators *why* a privileged call was rejected **without** revealing
//! how many signers are configured, which addresses are authorised, or
//! whether a particular caller is "close" to meeting the quorum.  Concrete
//! rules:
//!
//! - The reason variants are functional categories, not structural ones:
//!   `NotAdmin`, `NotGovernor`, `ContractLocked`, `QuorumNotMet`.
//!   None of them expose the threshold value or the signer list length.
//! - The emitted event records only the *caller* address supplied by the
//!   caller themselves (already public), never any internal authorisation
//!   state.
//! - `QuorumNotMet` is deliberately undifferentiated — it does not say
//!   "you were N signers short" or "these keys were unrecognised".  An
//!   attacker cannot use it to determine how many additional co-signers
//!   they need to recruit.
//!
//! # Event schema (locked by tests in `tests/`)
//!
//! ```text
//! topics : ["auth_denied", <reason_symbol>]
//! data   : { caller: Address }
//! ```
//!
//! Score-write events:
//!
//! ```text
//! topics : ["score_updated"]
//! data   : { wallet: Address, score: u32, ledger: u32 }
//! ```
//!
//! Parameter-change events:
//!
//! ```text
//! topics : ["param_updated", <param_name_symbol>]
//! data   : { new_value: u32, ledger: u32 }
//! ```

use soroban_sdk::{Address, Env, Symbol, symbol_short};

// ---------------------------------------------------------------------------
// Reason codes
// ---------------------------------------------------------------------------

/// Operator-actionable reason for an authorisation denial.
///
/// Each variant maps to a distinct on-chain symbol so event subscribers can
/// filter without parsing freeform strings.  The symbol names are part of the
/// public ABI — do **not** rename them without a migration note.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum AuthDeniedReason {
    /// The caller did not satisfy `require_auth()` for the admin role.
    NotAdmin,
    /// The call target requires the governor role; caller holds only admin.
    NotGovernor,
    /// The contract is in the locked state; no privileged writes are allowed.
    ContractLocked,
    /// A multi-party governance call did not reach the required quorum.
    /// Deliberately does not reveal threshold or signer-set size.
    QuorumNotMet,
}

impl AuthDeniedReason {
    /// Returns the short symbol written into the event topic.
    ///
    /// Symbol names are ≤ 9 ASCII characters (`symbol_short!` limit).
    pub fn as_symbol(&self) -> Symbol {
        match self {
            Self::NotAdmin       => symbol_short!("not_admin"),
            Self::NotGovernor    => symbol_short!("not_gov"),
            Self::ContractLocked => symbol_short!("locked"),
            Self::QuorumNotMet   => symbol_short!("no_quorum"),
        }
    }
}

// ---------------------------------------------------------------------------
// Event emitters
// ---------------------------------------------------------------------------

/// Emit an `auth_denied` event.
///
/// Called *before* the function returns an error or traps so the event is
/// included in the transaction's diagnostic stream even when the call reverts.
///
/// `caller` is the address the caller supplied — already public information.
/// No internal signer-set state is included.
pub fn emit_auth_denied(env: &Env, reason: AuthDeniedReason, caller: &Address) {
    let topics = (symbol_short!("auth_deny"), reason.as_symbol());
    // Data carries only the (public) caller address.
    env.events().publish(topics, caller.clone());
}

/// Emit a `score_updated` event after a successful privileged score write.
pub fn emit_score_updated(env: &Env, wallet: &Address, score: u32) {
    let topics = (symbol_short!("score_upd"),);
    let data = (wallet.clone(), score, env.ledger().sequence());
    env.events().publish(topics, data);
}

/// Emit a `param_updated` event after a successful governance parameter change.
pub fn emit_param_updated(env: &Env, param: Symbol, new_value: u32) {
    let topics = (symbol_short!("param_upd"), param);
    let data = (new_value, env.ledger().sequence());
    env.events().publish(topics, data);
}
