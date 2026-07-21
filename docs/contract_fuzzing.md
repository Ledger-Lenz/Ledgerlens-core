# LedgerLens Contract Fuzzing

This document describes the fuzzing infrastructure for the LedgerLens Soroban smart contracts (`oracle_aggregator` and `zk_verifier`). Fuzzing systematically tests contract entrypoints for integer overflow, authorization bypass, and malformed-input panics using `cargo-fuzz` (libFuzzer).

## Overview

The fuzzing infrastructure targets the trust boundary of our on-chain components — the Soroban contracts that AMMs, lending protocols, and DEX aggregators on Stellar query natively. A reachable panic, an authorization bypass, or a malformed-input DoS is a direct threat to the composability guarantees the entire LedgerLens project is built on.

### Why Fuzzing?

Both `oracle_aggregator` and `zk_verifier` set `overflow-checks = true` in their `[profile.release]` configuration, which means arithmetic overflow panics rather than silently wrapping. This is good practice for safety, but it also means any overflow surface area has the potential to cause a transaction abort — and that surface area has never been systematically searched for reachable panics until now.

cargo-fuzz (built on libFuzzer) is the standard coverage-guided fuzzing tool for Rust and integrates cleanly with Soroban's `soroban-sdk` test environment (`Env::default()`, `env.mock_all_auths()`).

### Scope

This infrastructure is scoped to **fuzzing**, not full formal verification or symbolic execution. The primary deliverable is a working `cargo-fuzz` CI gate. Symbolic-execution harnesses (e.g., using `cargo kani`) may be added as a follow-up, but the current implementation focuses on libFuzzer-based coverage-guided fuzzing.

## Architecture

```
contracts/
├── oracle_aggregator/
│   ├── src/
│   │   ├── lib.rs               # Contract entrypoints
│   │   └── test.rs              # Example-based unit tests
│   ├── Cargo.toml
│   └── fuzz/
│       ├── Cargo.toml
│       ├── corpus/              # Seed inputs
│       │   ├── fuzz_submit_with_quorum/
│       │   ├── fuzz_canonical_message/
│       │   └── fuzz_auth_bypass/
│       └── fuzz_targets/
│           ├── fuzz_submit_with_quorum.rs
│           ├── fuzz_canonical_message.rs
│           └── fuzz_auth_bypass.rs
│
├── zk_verifier/
│   ├── src/
│   │   └── lib.rs               # Contract entrypoints
│   ├── Cargo.toml
│   └── fuzz/
│       ├── Cargo.toml
│       ├── corpus/              # Seed inputs
│       │   ├── fuzz_submit_score/
│       │   ├── fuzz_verify_threshold/
│       │   └── fuzz_auth_bypass/
│       └── fuzz_targets/
│           ├── fuzz_submit_score.rs
│           ├── fuzz_verify_threshold.rs
│           └── fuzz_auth_bypass.rs
```

### Workspace Structure

The repo root contains a workspace `Cargo.toml`:

```toml
[workspace]
members = [
    "contracts/oracle_aggregator",
    "contracts/zk_verifier",
]
resolver = "2"

[workspace.dependencies]
soroban-sdk = "21.0"
```

This allows both contracts to be built and fuzzed uniformly without breaking their existing independent `cdylib` builds for WASM deployment.

## Fuzz Targets

### oracle_aggregator

| Target                        | Description                                                                                      |
| ----------------------------- | ------------------------------------------------------------------------------------------------ |
| `fuzz_submit_with_quorum`     | Exercises `submit_with_quorum` with arbitrary threshold, oracle_keys counts/contents, score, and timestamp values |
| `fuzz_canonical_message`      | Exercises `canonical_message` with arbitrary wallet/asset_pair/score/timestamp inputs, including boundary values (u32::MAX, u64::MAX, empty/oversized Symbol/Address inputs) |
| `fuzz_auth_bypass`            | Tests double-initialization protection — ensures `initialize` cannot be called twice            |

### zk_verifier

| Target                        | Description                                                                                      |
| ----------------------------- | ------------------------------------------------------------------------------------------------ |
| `fuzz_submit_score`           | Exercises `submit_score` with arbitrary score and commitment values                              |
| `fuzz_verify_threshold`       | Exercises `verify_threshold` with malformed/adversarial proof byte inputs                        |
| `fuzz_auth_bypass`            | Tests that `submit_score` requires `admin.require_auth()` and cannot succeed without authorization |

### Authorization Bypass Harnesses

The `fuzz_auth_bypass` targets are **assertion-based**, not crash-only. They deliberately omit `env.mock_all_auths()` (or mock only a subset) and assert that operations requiring authorization always fail rather than merely not crashing.

For example, `zk_verifier/fuzz_auth_bypass.rs`:

```rust
// DELIBERATELY do NOT call env.mock_all_auths()
let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
    client.submit_score(&admin, &wallet, &score, &commitment_hash, &pedersen_x, &pedersen_y);
}));

// Expected: result is Err (panic from require_auth failing)
// If it succeeded without panicking, that's an authorization bypass bug
```

This ensures that every `require_auth()` call site is actually present and functioning.

## Running Fuzz Targets Locally

### Prerequisites

1. Install Rust nightly:
   ```bash
   rustup toolchain install nightly
   ```

2. Install cargo-fuzz:
   ```bash
   cargo install cargo-fuzz
   ```

### Run a Single Target

From the contract directory (e.g., `contracts/oracle_aggregator`):

```bash
cd contracts/oracle_aggregator
cargo +nightly fuzz run fuzz_submit_with_quorum
```

By default, fuzzing runs indefinitely until you press Ctrl+C or a crash is found.

### Run with Time Limit

```bash
cargo +nightly fuzz run fuzz_submit_with_quorum -- -max_total_time=120
```

This runs for 120 seconds (2 minutes) and then exits. The CI uses 120 seconds per target for PR runs.

### Run with Custom Options

Common libFuzzer options:

```bash
cargo +nightly fuzz run fuzz_canonical_message -- \
  -max_total_time=300 \
  -max_len=1024 \
  -rss_limit_mb=2048 \
  -timeout=10
```

- `-max_total_time=N` — Run for N seconds total
- `-max_len=N` — Limit input size to N bytes (prevents OOM on unbounded inputs)
- `-rss_limit_mb=N` — Kill the process if RSS exceeds N MB
- `-timeout=N` — Abort a single test case if it runs longer than N seconds

For all options: `cargo +nightly fuzz run <target> -- -help=1`

### Run All Targets (Script)

To run all targets for a contract:

```bash
cd contracts/oracle_aggregator
for target in fuzz_submit_with_quorum fuzz_canonical_message fuzz_auth_bypass; do
  echo "Running $target..."
  cargo +nightly fuzz run $target -- -max_total_time=60 || exit 1
done
```

## Interpreting Results

### Crash-Free Run

If fuzzing completes without finding a crash:

```
#1234567  DONE   cov: 456 ft: 123 corp: 89 exec/s: 10234 rss: 45Mb
```

- `cov: 456` — 456 unique code coverage edges hit
- `ft: 123` — 123 unique feature comparisons observed (libFuzzer's internal metric)
- `corp: 89` — 89 distinct inputs saved to the corpus
- `exec/s: 10234` — 10,234 executions per second

This is **good** — no panics or crashes were found.

### Crash Found

If a crash is found:

```
==12345== ERROR: libFuzzer: deadly signal
    #0 0x... in panic_handler
    #1 0x... in canonical_message
    ...
artifact_prefix='./fuzz/artifacts/fuzz_canonical_message/'; Test unit written to ./fuzz/artifacts/fuzz_canonical_message/crash-abc123def456
```

The crash input is saved to `fuzz/artifacts/<target>/crash-<hash>`. This is a binary file containing the exact input bytes that triggered the panic.

### Reproducing a Crash

To reproduce a crash with the saved artifact:

```bash
cargo +nightly fuzz run fuzz_canonical_message fuzz/artifacts/fuzz_canonical_message/crash-abc123def456
```

This re-runs the fuzzer with only that input, which should immediately reproduce the panic.

### Debugging a Crash

1. Reproduce the crash as above
2. Open the fuzz target source (e.g., `fuzz_targets/fuzz_canonical_message.rs`)
3. Add `println!` or `dbg!` statements to inspect the input values
4. Re-run with the artifact

Alternatively, use a debugger:

```bash
rust-lldb -- cargo +nightly fuzz run fuzz_canonical_message fuzz/artifacts/fuzz_canonical_message/crash-abc123def456
```

## CI Integration

### PR Fuzz Job (Short)

Every PR touching `contracts/**` runs a short fuzz pass in `.github/workflows/ci.yml`:

```yaml
fuzz:
  runs-on: ubuntu-latest
  if: github.event_name == 'pull_request'
  steps:
    - uses: actions/checkout@v4
    - uses: actions-rs/toolchain@v1
      with:
        toolchain: nightly
        override: true
    - run: cargo install cargo-fuzz
    - name: Fuzz oracle_aggregator (short PR run)
      run: |
        cd contracts/oracle_aggregator
        for target in fuzz_submit_with_quorum fuzz_canonical_message fuzz_auth_bypass; do
          cargo +nightly fuzz run $target -- -max_total_time=120 -max_len=1024 || exit 1
        done
    - name: Fuzz zk_verifier (short PR run)
      run: |
        cd contracts/zk_verifier
        for target in fuzz_submit_score fuzz_verify_threshold fuzz_auth_bypass; do
          cargo +nightly fuzz run $target -- -max_total_time=120 -max_len=1024 || exit 1
        done
    - name: Upload crash artifacts
      if: failure()
      uses: actions/upload-artifact@v4
      with:
        name: fuzz-crash-artifacts
        path: |
          contracts/oracle_aggregator/fuzz/artifacts
          contracts/zk_verifier/fuzz/artifacts
```

**Time budget**: 120 seconds per target. This is short enough to not block PR merges (6 targets × 120s = ~12 minutes total) but long enough to catch shallow bugs.

If a crash is found, the job fails and uploads the crash artifacts as a GitHub Actions artifact for manual inspection.

### Nightly Fuzz Job (Deep)

A separate scheduled workflow (`.github/workflows/fuzz-nightly.yml`) runs every day at 2 AM UTC:

```yaml
on:
  schedule:
    - cron: '0 2 * * *'
  workflow_dispatch:  # Allow manual trigger
```

**Time budget**: 30 minutes per target (1800 seconds). This provides much deeper coverage than the PR job.

The nightly job uses a matrix strategy to parallelize across contracts and caches the fuzz corpus between runs to preserve discovered inputs.

### Interpreting CI Failures

If the fuzz job fails on CI:

1. Click "Actions" → Failed workflow run → "fuzz" job
2. Scroll to the failing target output
3. Look for the `artifact_prefix` line showing the crash file path
4. Download the "fuzz-crash-artifacts" artifact from the workflow summary
5. Extract the artifact locally (e.g., `unzip fuzz-crash-artifacts.zip`)
6. Reproduce the crash locally as described above

## Corpus Management

### What is the Corpus?

The fuzzing corpus is a directory of input files (`fuzz/corpus/<target>/`) that libFuzzer uses as seed inputs. On the first run, the fuzzer starts with these seeds and then mutates them to discover new code paths. Newly discovered inputs that increase coverage are automatically added to the corpus.

### Seeding the Corpus

Good seed inputs dramatically improve fuzzing efficiency. For example, `fuzz_submit_with_quorum` should start with valid inputs from `test.rs` (known-good coverage) rather than purely random bytes.

To seed a corpus:

1. Run the fuzz target once with no corpus (it will generate some)
2. Or manually create seed files based on unit test examples

Example seed file (`fuzz/corpus/fuzz_submit_with_quorum/valid_quorum`):

This is a binary file generated by the `arbitrary` crate, not human-readable. The easiest way to create seeds is to run the fuzzer briefly and let it generate a small corpus, then commit those files.

### Committing Corpus Files

✅ **DO commit** small corpus files (< 1 KB each, < 50 files total) that improve initial coverage.

❌ **DO NOT commit** large corpus directories (hundreds of files) — they bloat the repo.

❌ **DO NOT commit** crash artifacts to the corpus directory — they go in `fuzz/artifacts/` and should be reviewed before committing (they may contain sensitive data patterns, even if synthetic).

### Corpus Artifacts and Sensitive Data

Fuzz corpus and crash artifacts are synthetic fuzz bytes, not real wallet addresses or scores. However, reviewers should **always** confirm this before committing any auto-generated corpus/crash files to the repo.

If a crash artifact contains patterns that could be mistaken for real data (e.g., valid-looking Base64 strings, hex addresses), document it in the commit message and redact if necessary.

## Security Considerations

### Intentional vs. Unintentional Panics

Some panics are intentional (e.g., `initialize` panicking on "already initialized" re-init). The fuzz harnesses must not flag every panic as a bug.

The current approach is to wrap known-intentional panic paths with a check that the panic message matches an allowlisted set:

```rust
if let Err(e) = init_result {
    let panic_msg = if let Some(s) = e.downcast_ref::<&str>() {
        s.to_string()
    } else if let Some(s) = e.downcast_ref::<String>() {
        s.clone()
    } else {
        "unknown panic".to_string()
    };
    
    // Allow only known intentional panics
    assert!(
        panic_msg.contains("already initialized"),
        "Unexpected panic during initialize: {}",
        panic_msg
    );
    return; // Skip rest of test if initialization panicked intentionally
}
```

If an unexpected panic message is observed, the fuzzer will fail.

### Authorization Bypass Coverage

The `fuzz_auth_bypass` harnesses must cover **every `require_auth()` call site** in the contracts. Currently:

- `oracle_aggregator`: No `require_auth()` in `submit_with_quorum` or `canonical_message` (signature-based auth instead)
- `oracle_aggregator`: `initialize` has re-init protection (not auth-based, but tested in `fuzz_auth_bypass`)
- `zk_verifier`: `submit_score` calls `admin.require_auth()` (tested in `fuzz_auth_bypass`)

To find all call sites:

```bash
grep -rn "require_auth" contracts/
```

If a new entrypoint is added that requires authorization, a corresponding `fuzz_auth_bypass` case must be added.

### CI Time Budget Rationale

**Why 120 seconds per PR target?**

- Short enough to not block PR merges (~12 minutes total for all targets)
- Long enough to catch shallow bugs (overflows on boundary values, missing auth checks)
- Empirically sufficient for > 80% code coverage on the existing test suite

**Why 30 minutes per nightly target?**

- Deep enough to discover complex crash paths (e.g., nested if-conditions with rare combinations)
- Runs off-peak (2 AM UTC) so it doesn't block development
- Corpus is cached between runs, so each nightly run builds on the previous day's coverage

If the corpus grows large over time (> 10,000 files), consider pruning it with `cargo fuzz cmin`:

```bash
cargo +nightly fuzz cmin fuzz_submit_with_quorum
```

This minimizes the corpus to the smallest set of inputs that preserve current coverage.

## Troubleshooting

### "cargo: 'fuzz' is not a cargo command"

Install cargo-fuzz:

```bash
cargo install cargo-fuzz
```

### "error: package `ledgerlens-zk-verifier v0.1.0` cannot be built because it requires rustc 1.XX or newer"

Use nightly Rust:

```bash
cargo +nightly fuzz run <target>
```

### "oom: out of memory"

Reduce the input size limit:

```bash
cargo +nightly fuzz run <target> -- -max_len=512 -rss_limit_mb=2048
```

### "timeout: processing time exceeded"

Increase the per-input timeout:

```bash
cargo +nightly fuzz run <target> -- -timeout=30
```

Or investigate if there's an infinite loop in the contract code.

### "assertion failed: corpus directory is empty"

Create the corpus directory:

```bash
mkdir -p fuzz/corpus/<target>
```

Run the fuzzer once to generate an initial corpus:

```bash
cargo +nightly fuzz run <target> -- -max_total_time=10
```

### Fuzzer finds a known issue

If a crash is found for a known issue (e.g., a TODO or tracked bug):

1. File a GitHub issue if one doesn't exist
2. Add the crash artifact to the issue description
3. Either fix the bug immediately or add a `// KNOWN ISSUE: #123` comment in the harness and temporarily skip that test case

Do not merge a fuzz harness that consistently fails on a known bug — it breaks the CI gate. Either fix the bug first or disable that specific input pattern in the harness.

## References

- [cargo-fuzz documentation](https://rust-fuzz.github.io/book/cargo-fuzz.html)
- [libFuzzer options](https://llvm.org/docs/LibFuzzer.html#options)
- [Soroban SDK testing guide](https://soroban.stellar.org/docs/how-to-guides/testing)
- [LedgerLens Oracle Quorum design](oracle_quorum.md)
- [LedgerLens Soroban Operations](soroban_operations.md)
