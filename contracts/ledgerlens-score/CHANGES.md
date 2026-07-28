# ledgerlens-score — ABI, Event, Error, and Storage Compatibility Notes

> This document satisfies the acceptance criterion: *"Public ABI, event,
> error, and storage compatibility impacts are explicitly documented in the
> PR."*  It is the authoritative reference for off-chain SDK authors,
> indexers, and operators integrating with this contract.

---

## Status

**New contract — no prior deployment.**  There is no existing on-chain state
to migrate.  All schemas defined below are the initial version and must be
treated as stable from first deployment.

---

## 1. Public ABI

### Entry points

| Function | Arguments | Returns | Auth required |
|---|---|---|---|
| `initialize(admin, governor)` | `Address, Address` | `Result<(), Error>` | None (bootstraps roles) |
| `submit_score(wallet, score)` | `Address, u32` | `Result<(), Error>` | None (oracle path) |
| `get_score(wallet)` | `Address` | `Option<u32>` | None |
| `set_admin(caller, new_admin)` | `Address, Address` | `Result<(), Error>` | Admin |
| `lock(caller)` | `Address` | `Result<(), Error>` | Admin |
| `unlock(caller)` | `Address` | `Result<(), Error>` | Admin |
| `set_governor(caller, new_governor)` | `Address, Address` | `Result<(), Error>` | Admin |
| `set_score_max(caller, new_max)` | `Address, u32` | `Result<(), Error>` | Governor |
| `set_score_floor(caller, new_floor)` | `Address, u32` | `Result<(), Error>` | Governor |
| `is_locked()` | — | `bool` | None |
| `get_score_max()` | — | `u32` | None |
| `get_score_floor()` | — | `u32` | None |
| `get_param_version()` | — | `u32` | None |

**ABI stability rule:** entry-point names and argument types are frozen from
first deployment.  New entry points may be appended; existing ones must not
be removed or have their signatures changed without a contract upgrade.

---

## 2. Error codes

Error codes are encoded as `ScVal::Error::Contract(n)` in the XDR response.
Off-chain clients decode these values.  **Codes are stable and append-only.**

| Code | Name | Meaning |
|---|---|---|
| 1 | `NotAdmin` | Caller did not satisfy the admin role check |
| 2 | `NotGovernor` | Caller does not hold governor privileges |
| 3 | `ContractLocked` | Contract is locked; privileged writes refused |
| 4 | `QuorumNotMet` | Multi-party quorum not satisfied (reserved; not yet raised by any current call path) |
| 5 | `AlreadyInitialized` | `initialize` called more than once |
| 6 | `InvalidParameter` | A parameter value is outside the allowed range |
| 7 | `ScoreOutOfRange` | `score > score_max` |

**Migration rule:** never reorder or delete a code.  Append new codes at the
next integer.

---

## 3. Events

All events are published via `env.events().publish(topics, data)`.

### 3.1 `auth_deny` — authorization denied

Emitted on **every** failed privileged call, including failures that
subsequently cause the transaction to revert (the event still appears in the
diagnostic stream).

```
topics[0] : Symbol  = "auth_deny"          (symbol_short, 9 chars)
topics[1] : Symbol  = <reason>             (see table below)
data      : Address = <caller>             (the address passed by the caller)
```

Reason symbols:

| Symbol | Meaning |
|---|---|
| `not_admin` | Caller is not the admin |
| `not_gov` | Caller is not the governor |
| `locked` | Contract is administratively locked |
| `no_quorum` | Multi-party quorum not met (reserved) |

**Security contract:** topics always contain exactly two elements.  No third
topic may be added that could expose threshold values, signer-set size, or
any indicator of "closeness" to a quorum.  The `data` field contains only the
caller address (public knowledge).  Tests in `src/tests.rs` assert
`topics.len() == 2` and inspect the exact data payload.

### 3.2 `score_upd` — score recorded

Emitted on every successful `submit_score` call.

```
topics[0] : Symbol  = "score_upd"
data      : (Address, u32, u32) = (wallet, score, ledger_sequence)
```

### 3.3 `param_upd` — governance parameter changed

Emitted on every successful governance write (`set_admin`, `lock`, `unlock`,
`set_governor`, `set_score_max`, `set_score_floor`).

```
topics[0] : Symbol  = "param_upd"
topics[1] : Symbol  = <param_name>         (storage key symbol, e.g. "SCORE_MAX")
data      : (u32, u32) = (new_value, ledger_sequence)
```

For address-typed parameters (`ADMIN`, `GOVERNOR`) `new_value` is set to `0`
as a sentinel; the new address is retrievable via the corresponding view
function.

---

## 4. Storage layout

All keys live in **instance storage** (bounded by contract lifetime; no TTL
tuning required).

| Key symbol | Rust type | Default | Purpose |
|---|---|---|---|
| `ADMIN` | `Address` | set at `initialize` | Current admin |
| `GOVERNOR` | `Address` | set at `initialize` | Current governor |
| `LOCKED` | `bool` | `false` | Lock state |
| `SCORE_MAX` | `u32` | `100` | Maximum accepted score |
| `S_FLOOR` | `u32` | `0` | Score floor |
| `PARAM_VER` | `u32` | `0` | Monotone parameter version |
| `SCORES` | `Map<Address, u32>` | empty | Per-wallet risk scores |

**Migration note:** storage keys are `symbol_short!` constants defined in
`parameter_governance.rs` and `lib.rs`.  Renaming a key is a breaking change
requiring a migration entry point that reads from the old key and writes to
the new one before deleting the old.

---

## 5. Resource usage

- **Instance storage** is used exclusively.  Every write is O(1) or O(log n)
  for the `SCORES` map.  The map grows at one entry per unique wallet; no
  bounded eviction is implemented at this layer (relies on Soroban's native
  storage cost metering and ledger TTL).
- **Event emission** is O(1) per call regardless of signer-set size — no
  signer list iteration occurs in the telemetry path.
- **Worst-case invocation:** `submit_score` performs one lock read, one
  score-max read, one map read-modify-write, and one event publish.  All
  host operations are individually metered by the Soroban host.

---

## 6. What did not change

This contract is a greenfield addition.  The existing `oracle_aggregator` and
`zk_verifier` contracts are **not modified** by this PR.  Their storage,
events, errors, and entry points are unchanged.

---

## 7. Future compatibility commitments

- Error codes 1–7 are frozen.
- Event topic symbol names (`auth_deny`, `not_admin`, `not_gov`, `locked`,
  `no_quorum`, `score_upd`, `param_upd`) are frozen.
- Storage key symbols listed in §4 are frozen.
- The `AuthDeniedReason` Rust enum is not part of the on-chain ABI but its
  mapping to symbols (§3.1) is frozen.
- `SCORE_HARD_MAX = 100` is a compile-time constant; changing it requires a
  new contract version.
