//! Contract error codes for ledgerlens-score.
//!
//! # ABI stability
//!
//! The numeric discriminants assigned below are part of the public contract
//! ABI.  Off-chain clients (SDKs, indexers) decode these codes out of
//! `ScVal::Error::Contract(n)`.  **Never reorder or delete** a variant once
//! deployed; only append new ones at the end.
//!
//! | Code | Name              | Meaning                                       |
//! |------|-------------------|-----------------------------------------------|
//! | 1    | NotAdmin          | Caller did not satisfy the admin role         |
//! | 2    | NotGovernor       | Caller does not hold governor privileges      |
//! | 3    | ContractLocked    | Contract is administratively locked           |
//! | 4    | QuorumNotMet      | Multi-party quorum was not satisfied          |
//! | 5    | AlreadyInitialized| Attempted double-initialisation               |
//! | 6    | InvalidParameter  | A supplied parameter value is out of range    |
//! | 7    | ScoreOutOfRange   | Score value exceeds the allowed maximum       |

use soroban_sdk::contracterror;

#[contracterror]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum LedgerLensError {
    /// Caller did not satisfy `require_auth()` for the admin role.
    NotAdmin = 1,
    /// Caller does not hold governor privileges.
    NotGovernor = 2,
    /// Contract is administratively locked; privileged writes are refused.
    ContractLocked = 3,
    /// Multi-party governance quorum was not satisfied.
    QuorumNotMet = 4,
    /// Attempted double-initialisation.
    AlreadyInitialized = 5,
    /// A supplied parameter value is out of the allowed range.
    InvalidParameter = 6,
    /// Score value exceeds the maximum of 100.
    ScoreOutOfRange = 7,
}
