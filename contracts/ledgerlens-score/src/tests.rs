//! Deterministic tests for the ledgerlens-score contract.
//!
//! # Coverage goals
//!
//! 1. **Success paths** — happy-path invocations that must succeed and emit
//!    the correct events.
//! 2. **Boundary cases** — edge values (score == score_max, floor == 0,
//!    param version monotonicity, lock/unlock round-trip).
//! 3. **Adversarial failure modes** — wrong caller, double-init, locked
//!    writes, out-of-range scores, role confusion.
//! 4. **Event schema lock** — every emitted event is asserted by topic and
//!    data so a schema change breaks the tests.
//! 5. **Signer-set opacity** — failure events must not contain threshold
//!    or signer-set size; asserted by inspecting raw event payloads.

#![cfg(test)]

extern crate std;

use soroban_sdk::{
    testutils::{Address as _, Events},
    Address, Env, IntoVal,
};

use super::{LedgerLensScore, LedgerLensScoreClient};
use crate::errors::LedgerLensError;

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/// Bootstrap a fresh environment with the contract deployed and initialised.
fn setup() -> (Env, LedgerLensScoreClient<'static>, Address, Address) {
    let env = Env::default();
    // Allow all require_auth() calls to succeed for addresses under test.
    env.mock_all_auths();

    let contract_id = env.register_contract(None, LedgerLensScore);
    let client = LedgerLensScoreClient::new(&env, &contract_id);

    let admin = Address::generate(&env);
    let governor = Address::generate(&env);

    client.initialize(&admin, &governor).unwrap();

    (env, client, admin, governor)
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

#[test]
fn test_initialize_sets_defaults() {
    let (env, client, _admin, _governor) = setup();

    assert_eq!(client.get_score_max(), 100);
    assert_eq!(client.get_score_floor(), 0);
    assert_eq!(client.get_param_version(), 0);
    assert!(!client.is_locked());
}

#[test]
fn test_double_initialize_fails() {
    let (env, client, admin, governor) = setup();
    let result = client.try_initialize(&admin, &governor);
    assert_eq!(
        result.unwrap_err().unwrap(),
        LedgerLensError::AlreadyInitialized
    );
}

// ---------------------------------------------------------------------------
// Score submission — success paths
// ---------------------------------------------------------------------------

#[test]
fn test_submit_score_success() {
    let (env, client, _admin, _governor) = setup();
    let wallet = Address::generate(&env);

    client.submit_score(&wallet, &75).unwrap();
    assert_eq!(client.get_score(&wallet), Some(75));
}

#[test]
fn test_submit_score_at_max_boundary() {
    let (env, client, _admin, _governor) = setup();
    let wallet = Address::generate(&env);

    // Exactly at score_max (100) must succeed.
    client.submit_score(&wallet, &100).unwrap();
    assert_eq!(client.get_score(&wallet), Some(100));
}

#[test]
fn test_submit_score_zero() {
    let (env, client, _admin, _governor) = setup();
    let wallet = Address::generate(&env);

    client.submit_score(&wallet, &0).unwrap();
    assert_eq!(client.get_score(&wallet), Some(0));
}

#[test]
fn test_submit_score_emits_score_updated_event() {
    let (env, client, _admin, _governor) = setup();
    let wallet = Address::generate(&env);

    env.events().all(); // clear prior events
    client.submit_score(&wallet, &42).unwrap();

    let events = env.events().all();
    // Last event should be score_updated.
    let (_, topics, data) = events.last().expect("expected at least one event");

    // Topic 0 must be the "score_upd" symbol — schema lock.
    let topic0: soroban_sdk::Symbol = topics.get(0).unwrap();
    assert_eq!(topic0, soroban_sdk::symbol_short!("score_upd"));

    // Data encodes (wallet, score, ledger_sequence).
    let (ev_wallet, ev_score, _ev_seq): (Address, u32, u32) = data.into_val(&env);
    assert_eq!(ev_wallet, wallet);
    assert_eq!(ev_score, 42u32);
}

// ---------------------------------------------------------------------------
// Score submission — adversarial / boundary failures
// ---------------------------------------------------------------------------

#[test]
fn test_submit_score_above_max_fails() {
    let (env, client, _admin, _governor) = setup();
    let wallet = Address::generate(&env);

    let result = client.try_submit_score(&wallet, &101);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::ScoreOutOfRange);
}

#[test]
fn test_submit_score_above_lowered_max_fails() {
    let (env, client, _admin, governor) = setup();
    let wallet = Address::generate(&env);

    // Lower the max to 50.
    client.set_score_max(&governor, &50).unwrap();

    let result = client.try_submit_score(&wallet, &51);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::ScoreOutOfRange);
}

#[test]
fn test_submit_score_when_locked_emits_auth_denied() {
    let (env, client, admin, _governor) = setup();
    let wallet = Address::generate(&env);

    client.lock(&admin).unwrap();

    env.events().all(); // clear
    let result = client.try_submit_score(&wallet, &50);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::ContractLocked);

    // An auth_denied event must have been emitted.
    let events = env.events().all();
    let (_, topics, data) = events.last().expect("expected auth_denied event");

    let topic0: soroban_sdk::Symbol = topics.get(0).unwrap();
    assert_eq!(topic0, soroban_sdk::symbol_short!("auth_deny"));

    let reason: soroban_sdk::Symbol = topics.get(1).unwrap();
    assert_eq!(reason, soroban_sdk::symbol_short!("locked"));

    // Data must be the caller address only — no threshold, no signer-set size.
    let ev_caller: Address = data.into_val(&env);
    assert_eq!(ev_caller, wallet);
}

// ---------------------------------------------------------------------------
// Locking
// ---------------------------------------------------------------------------

#[test]
fn test_lock_unlock_round_trip() {
    let (env, client, admin, _governor) = setup();

    assert!(!client.is_locked());
    client.lock(&admin).unwrap();
    assert!(client.is_locked());
    client.unlock(&admin).unwrap();
    assert!(!client.is_locked());
}

#[test]
fn test_lock_by_non_admin_fails_with_event() {
    let (env, client, _admin, _governor) = setup();
    let attacker = Address::generate(&env);

    env.events().all(); // clear
    let result = client.try_lock(&attacker);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::NotAdmin);

    // Verify auth_denied / not_admin was emitted.
    let events = env.events().all();
    let (_, topics, data) = events.last().expect("expected auth_denied event");

    let topic0: soroban_sdk::Symbol = topics.get(0).unwrap();
    assert_eq!(topic0, soroban_sdk::symbol_short!("auth_deny"));

    let reason: soroban_sdk::Symbol = topics.get(1).unwrap();
    assert_eq!(reason, soroban_sdk::symbol_short!("not_admin"));

    // Caller address is in data — NOT the real admin, just the attacker.
    let ev_caller: Address = data.into_val(&env);
    assert_eq!(ev_caller, attacker);

    // SCHEMA OPACITY CHECK: topics must have exactly 2 elements.
    // A third element would risk leaking signer-set information.
    assert_eq!(topics.len(), 2);
}

#[test]
fn test_unlock_by_non_admin_fails_with_event() {
    let (env, client, admin, _governor) = setup();
    let attacker = Address::generate(&env);

    client.lock(&admin).unwrap();
    env.events().all(); // clear

    let result = client.try_unlock(&attacker);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::NotAdmin);

    let events = env.events().all();
    let (_, topics, _) = events.last().expect("expected auth_denied event");
    let reason: soroban_sdk::Symbol = topics.get(1).unwrap();
    assert_eq!(reason, soroban_sdk::symbol_short!("not_admin"));
}

// ---------------------------------------------------------------------------
// Admin transfer
// ---------------------------------------------------------------------------

#[test]
fn test_set_admin_success() {
    let (env, client, admin, _governor) = setup();
    let new_admin = Address::generate(&env);

    client.set_admin(&admin, &new_admin).unwrap();

    // Old admin can no longer perform admin operations.
    let result = client.try_lock(&admin);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::NotAdmin);

    // New admin can.
    client.lock(&new_admin).unwrap();
}

#[test]
fn test_set_admin_by_non_admin_fails() {
    let (env, client, _admin, _governor) = setup();
    let attacker = Address::generate(&env);
    let fake_target = Address::generate(&env);

    let result = client.try_set_admin(&attacker, &fake_target);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::NotAdmin);
}

// ---------------------------------------------------------------------------
// Governor operations
// ---------------------------------------------------------------------------

#[test]
fn test_set_score_max_success_and_version_bump() {
    let (_env, client, _admin, governor) = setup();

    let v0 = client.get_param_version();
    client.set_score_max(&governor, &80).unwrap();
    assert_eq!(client.get_score_max(), 80);
    assert_eq!(client.get_param_version(), v0 + 1);
}

#[test]
fn test_set_score_max_emits_param_updated() {
    let (env, client, _admin, governor) = setup();

    env.events().all(); // clear
    client.set_score_max(&governor, &80).unwrap();

    let events = env.events().all();
    let (_, topics, data) = events.last().expect("expected param_updated event");

    let topic0: soroban_sdk::Symbol = topics.get(0).unwrap();
    assert_eq!(topic0, soroban_sdk::symbol_short!("param_upd"));

    let param: soroban_sdk::Symbol = topics.get(1).unwrap();
    assert_eq!(param, soroban_sdk::symbol_short!("SCORE_MAX"));

    let (ev_val, _ev_seq): (u32, u32) = data.into_val(&env);
    assert_eq!(ev_val, 80u32);
}

#[test]
fn test_set_score_max_zero_fails() {
    let (_env, client, _admin, governor) = setup();
    let result = client.try_set_score_max(&governor, &0);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::InvalidParameter);
}

#[test]
fn test_set_score_max_above_hard_max_fails() {
    let (_env, client, _admin, governor) = setup();
    let result = client.try_set_score_max(&governor, &101);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::InvalidParameter);
}

#[test]
fn test_set_score_floor_must_be_below_max() {
    let (_env, client, _admin, governor) = setup();

    // floor == max is rejected (must be strictly less).
    let result = client.try_set_score_floor(&governor, &100);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::InvalidParameter);

    // floor < max is accepted.
    client.set_score_floor(&governor, &10).unwrap();
    assert_eq!(client.get_score_floor(), 10);
}

#[test]
fn test_set_score_max_by_non_governor_fails_with_event() {
    let (env, client, _admin, _governor) = setup();
    let attacker = Address::generate(&env);

    env.events().all(); // clear
    let result = client.try_set_score_max(&attacker, &50);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::NotGovernor);

    let events = env.events().all();
    let (_, topics, data) = events.last().expect("expected auth_denied event");

    let topic0: soroban_sdk::Symbol = topics.get(0).unwrap();
    assert_eq!(topic0, soroban_sdk::symbol_short!("auth_deny"));

    let reason: soroban_sdk::Symbol = topics.get(1).unwrap();
    assert_eq!(reason, soroban_sdk::symbol_short!("not_gov"));

    // Caller must be the attacker — not the real governor.
    let ev_caller: Address = data.into_val(&env);
    assert_eq!(ev_caller, attacker);

    // OPACITY: exactly 2 topics, no signer-set info.
    assert_eq!(topics.len(), 2);
}

#[test]
fn test_governor_op_while_locked_emits_auth_denied_locked() {
    let (env, client, admin, governor) = setup();

    client.lock(&admin).unwrap();
    env.events().all(); // clear

    let result = client.try_set_score_max(&governor, &80);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::ContractLocked);

    let events = env.events().all();
    let (_, topics, _) = events.last().expect("expected auth_denied event");
    let reason: soroban_sdk::Symbol = topics.get(1).unwrap();
    assert_eq!(reason, soroban_sdk::symbol_short!("locked"));
}

// ---------------------------------------------------------------------------
// Role confusion — admin cannot act as governor
// ---------------------------------------------------------------------------

#[test]
fn test_admin_cannot_set_score_max_directly() {
    let (env, client, admin, _governor) = setup();
    // Admin address is not the governor, so this must be rejected.
    let result = client.try_set_score_max(&admin, &50);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::NotGovernor);
}

#[test]
fn test_governor_cannot_lock_contract() {
    let (env, client, _admin, governor) = setup();
    // Governor does not hold admin rights, so lock must be rejected.
    let result = client.try_lock(&governor);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::NotAdmin);
}

// ---------------------------------------------------------------------------
// Parameter version monotonicity
// ---------------------------------------------------------------------------

#[test]
fn test_param_version_monotone_across_multiple_updates() {
    let (_env, client, _admin, governor) = setup();

    let v0 = client.get_param_version();
    client.set_score_max(&governor, &90).unwrap();
    client.set_score_floor(&governor, &5).unwrap();
    client.set_score_max(&governor, &85).unwrap();

    assert_eq!(client.get_param_version(), v0 + 3);
}

// ---------------------------------------------------------------------------
// Score unknown wallet
// ---------------------------------------------------------------------------

#[test]
fn test_get_score_unknown_wallet_returns_none() {
    let (env, client, _admin, _governor) = setup();
    let unknown = Address::generate(&env);
    assert_eq!(client.get_score(&unknown), None);
}

// ---------------------------------------------------------------------------
// Governor reassignment
// ---------------------------------------------------------------------------

#[test]
fn test_set_governor_transfers_role() {
    let (env, client, admin, _old_governor) = setup();
    let new_governor = Address::generate(&env);

    client.set_governor(&admin, &new_governor).unwrap();

    // New governor can update params.
    client.set_score_max(&new_governor, &70).unwrap();
    assert_eq!(client.get_score_max(), 70);
}

#[test]
fn test_set_governor_while_locked_fails() {
    let (env, client, admin, governor) = setup();
    let new_governor = Address::generate(&env);

    client.lock(&admin).unwrap();
    let result = client.try_set_governor(&admin, &new_governor);
    assert_eq!(result.unwrap_err().unwrap(), LedgerLensError::ContractLocked);
}
