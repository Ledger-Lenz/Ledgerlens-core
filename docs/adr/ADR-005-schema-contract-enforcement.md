# ADR-005: Cross-Language Schema Contract Enforcement

**Status**: Accepted  
**Date**: 2026-08-25  
**Deciders**: LedgerLens core team  
**Context**: [Issue audit evidence – see README.md §Shared Contracts]

---

## Context and Problem Statement

Three independent language ecosystems (Python/Pydantic, Rust/serde, TypeScript/Zod) each maintain their own copy of the `RiskScore`, `Trade`, and `Asset` schemas. The existing enforcement mechanism is a README warning and developer discipline. During an audit the following divergences were found:

| Field | Python core | Python SDK | TypeScript | Rust SDK | Proto |
|---|---|---|---|---|---|
| `latency_ms` | ✅ | ❌ missing | ❌ missing | ❌ missing | ❌ missing |
| `disputed` | ✅ | ✅ | ✅ | ✅ | ❌ missing |
| `prediction_set` | `list[int]` | `list[int]` | `list[int]` | `Vec<u8>` ⚠️ | ❌ missing |

Additionally, `crates/ledgerlens-sdk/README.md` claimed a shared fixture file at `tests/fixtures/zk_proof_vectors.json` was "consumed by both the Python and Rust test suites" for cross-language contract testing. That file exists but contains only ZK proof vectors — the claim was false, and no cross-language schema contract test existed.

---

## Decision Drivers

* **Zero-ambiguity divergence detection**: introducing a field name or type change in one language must cause a CI failure, not just a documentation note.
* **No new build toolchain**: the project already uses Python (pytest), Rust (cargo test), and TypeScript (vitest). Any solution must work within those existing runners.
* **Single source of truth**: one authoritative JSON document defines the contract; each language's tests prove it can round-trip that document faithfully.
* **Proto as partial reference**: `proto/ledgerlens/v1/scoring.proto` is the existing closest thing to a canonical schema. It is used as a cross-reference but not as the single source of truth because it currently has gaps (`disputed`, `prediction_set`, `latency_ms`) and generating per-language bindings from it would require introducing `protoc` into the TypeScript and Python test pipelines — a heavier toolchain change than needed.
* **Divergence, not just existence**: a "the fixture file exists and both languages can parse it" test is insufficient. Tests must prove that a deliberate field-name change in one implementation causes a CI failure.

---

## Considered Options

### Option A – Protobuf Codegen (single-source proto → generated stubs)

Generate language bindings from `proto/ledgerlens/v1/scoring.proto` for all three languages, replacing the hand-written models.

**Pros**: True single source of truth; field changes are structural, not textual.  
**Cons**: `scoring.proto` currently has three missing fields and would need updating first. Integrating `protoc` into the TypeScript SDK build and Python SDK packaging adds non-trivial toolchain complexity. The on-chain Soroban contract interface cannot be derived from proto, so a second SSoT would still be needed for the Rust/Soroban layer.

**Decision**: Rejected for initial implementation. The proto file should be kept up-to-date as a reference (and its gaps are corrected in this ADR's implementation), but codegen is deferred.

### Option B – Shared JSON fixture file + per-language round-trip tests (chosen)

A single `tests/fixtures/contract_vectors.json` file contains canonical serialized instances of `RiskScore`, `Trade`, and `Asset` in exactly the shape any language must produce/consume. Each language's test suite:

1. Deserializes every vector in the file using its own model.
2. Re-serializes to a normalized JSON object.
3. Asserts byte-for-byte (field name and type) equality against the canonical vector.
4. Includes an "adversarial" vector with a wrong field name to confirm the deserializer rejects or remaps it.

A Python CI script (`scripts/check_contract_vectors.py`) independently validates that the canonical file itself is consistent with `detection/risk_score.py` and `ingestion/data_models.py`. This acts as the "source of truth" gate: the Python core models are authoritative; all other implementations must match them.

**Pros**: No new build tools; works with existing pytest/cargo/vitest. Divergence detection is provable: breaking a field name fails the round-trip assertion. The fixture file is human-readable and diff-friendly in PRs.  
**Cons**: Fixture file must be updated when the schema changes; relies on CI discipline to keep it current. Mitigated by the Python CI gate that compares the fixture against the canonical Python models.

**Decision**: Accepted.

---

## Decision

**Implement Option B**: fixture-based contract testing with a Python-authoritative canonical fixture file and per-language round-trip tests.

### Implementation plan

1. **Canonical fixture file** (`tests/fixtures/contract_vectors.json`):
   - Contains one complete `RiskScore` vector with all fields populated (including v2+ uncertainty fields and `latency_ms`).
   - Contains one minimal `RiskScore` vector (core fields only, optionals null).
   - Contains one `Trade` vector (orderbook type).
   - Contains one `Asset` vector (XLM native) and one (issued credit).
   - Contains one adversarial vector proving the suite catches wrong field names.
   - Schema version field (`_contract_version`) allows future evolution tracking.

2. **Python contract tests** (`tests/test_contract_vectors.py`):
   - Load fixture; deserialize via `detection.risk_score.RiskScore` and `ingestion.data_models.{Trade,Asset}`.
   - Verify all fields present and typed correctly.
   - Verify adversarial vector (wrong field name) causes `ValidationError`.
   - Verify v2+ uncertainty fields (`score_lower`, `score_upper`, `prediction_set`, `coverage_guarantee`) are present and typed.

3. **Rust contract tests** (`crates/ledgerlens-sdk/tests/contract_vectors_test.rs`):
   - Load `../../tests/fixtures/contract_vectors.json` (relative path from workspace root).
   - Deserialize via `serde_json` into `ledgerlens_sdk::models::RiskScore`.
   - Assert all required fields present.
   - Fix `prediction_set` type from `Vec<u8>` to `Vec<i32>` to match Python canonical type.

4. **TypeScript contract tests** (`sdk/tests/contract_vectors.test.ts`):
   - Load fixture file via `fs.readFileSync`.
   - Validate via `RiskScoreSchema.parse()`.
   - Assert all fields present including `latency_ms`.
   - Fix `latency_ms` missing from `RiskScoreSchema`.

5. **CI schema drift check** (`.github/workflows/schema.yml`, new job `contract-vectors`):
   - Runs `scripts/check_contract_vectors.py` which:
     - Extracts the canonical field list from `detection.risk_score.RiskScore.model_json_schema()`.
     - Compares against every vector in `tests/fixtures/contract_vectors.json`.
     - Emits a diff identifying which fields are in the canonical model but absent from the fixture, and which languages are therefore out of sync.
   - Runs in the same CI job as the existing OpenAPI drift check.

6. **Zero-assertion safeguard** (`.github/workflows/cross_repo_e2e.yml`):
   - After the pytest step, check `pytest --co -q` reports ≥ 1 test collected.
   - If all tests were skipped, fail the job with an explicit error message.

### Divergence correction

As part of this ADR's implementation:
- `latency_ms` is added to `sdk/src/schemas.ts` (TypeScript).
- `latency_ms` is added to `packages/ledgerlens-sdk/src/ledgerlens/models.py` (Python SDK).
- `prediction_set` type in `crates/ledgerlens-sdk/src/models.rs` is corrected from `Option<Vec<u8>>` to `Option<Vec<i32>>`.
- `latency_ms` is added to `crates/ledgerlens-sdk/src/models.rs`.
- `proto/ledgerlens/v1/scoring.proto` is updated to add `disputed`, `prediction_set`, `latency_ms` fields.

### Advancement policy

The fixture file `tests/fixtures/contract_vectors.json` carries a `_contract_version` string (semver). When any shared field changes:

1. Update `detection/risk_score.py` or `ingestion/data_models.py` (Python core is authoritative).
2. Run `python scripts/generate_contract_vectors.py` to regenerate the canonical fixture from live Python models.
3. Update the matching field in all other language implementations.
4. CI will fail until all four language test suites pass against the new fixture.

---

## Consequences

**Positive**:
- Any field-name or type mismatch between Python core and any other language immediately fails CI.
- New contributors receive a CI failure with a message identifying which language is out of sync (the fixture diff identifies the field; the failing test language identifies the implementation).
- The `crates/ledgerlens-sdk/README.md` claim about a shared fixture is now true.
- The v2+ uncertainty fields are covered by the enforcement mechanism.

**Negative**:
- The fixture file requires an additional manual update step when the schema changes. Mitigated by `scripts/generate_contract_vectors.py` and the CI gate that catches fixture-vs-model skew.
- TypeScript tests require `fs` access in vitest; a `tsconfig` path alias or relative-path resolution is needed.

**Neutral**:
- The Soroban on-chain `RiskScore` struct (in `ledgerlens-contracts`) cannot be directly tested from this repo. The contract_vectors fixture serves as the reference document for that repo's maintainers; the `test_risk_score_schema_drift` E2E test will validate the API response shape once a real contract is deployed.
