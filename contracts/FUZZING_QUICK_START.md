# Contract Fuzzing Quick Start

Quick reference for running and debugging fuzz tests on the LedgerLens Soroban contracts.

## Prerequisites

```bash
rustup toolchain install nightly
cargo install cargo-fuzz
```

## Run All Targets (2 minutes each)

**oracle_aggregator:**
```bash
cd contracts/oracle_aggregator
for target in fuzz_submit_with_quorum fuzz_canonical_message fuzz_auth_bypass; do
  cargo +nightly fuzz run $target -- -max_total_time=120
done
```

**zk_verifier:**
```bash
cd contracts/zk_verifier
for target in fuzz_submit_score fuzz_verify_threshold fuzz_auth_bypass; do
  cargo +nightly fuzz run $target -- -max_total_time=120
done
```

## Run a Single Target

```bash
cd contracts/oracle_aggregator
cargo +nightly fuzz run fuzz_submit_with_quorum
```

Press **Ctrl+C** to stop (runs indefinitely until crash found).

## Common Options

```bash
# Run for 5 minutes
cargo +nightly fuzz run <target> -- -max_total_time=300

# Limit input size (prevent OOM)
cargo +nightly fuzz run <target> -- -max_len=512

# Run with multiple workers (parallel)
cargo +nightly fuzz run <target> -- -workers=4

# Show all options
cargo +nightly fuzz run <target> -- -help=1
```

## Reproduce a Crash

If a crash is found, it's saved to `fuzz/artifacts/<target>/crash-<hash>`:

```bash
# Re-run with the crash file
cargo +nightly fuzz run fuzz_submit_with_quorum \
  fuzz/artifacts/fuzz_submit_with_quorum/crash-abc123def456
```

## Debug a Crash

Add `println!` or `dbg!` to the fuzz target source:

```rust
fuzz_target!(|input: FuzzInput| {
    dbg!(&input);  // Print the input that caused the crash
    // ... rest of harness
});
```

Then re-run with the crash file.

## Minimize a Crash

Find the smallest input that still triggers the crash:

```bash
cargo +nightly fuzz tmin <target> fuzz/artifacts/<target>/crash-<hash>
```

## Check Code Coverage

```bash
cargo +nightly fuzz coverage <target>
```

Then open the HTML report:

```bash
open fuzz/coverage/<target>/index.html  # macOS
xdg-open fuzz/coverage/<target>/index.html  # Linux
```

## Minimize Corpus

If the corpus grows too large (> 10,000 files):

```bash
cargo +nightly fuzz cmin <target>
```

This keeps only the inputs that provide unique coverage.

## CI Behavior

**On every PR** (`CI / Contract fuzz (PR smoke)` in `.github/workflows/ci.yml`):
- This is a **required status check** on `main` — contract changes cannot merge
  unless it passes. It runs on every PR (no path filter) so it is never left pending.
- Every committed regression input is replayed first (see below)
- All 6 targets then run for 120 seconds each (~12 min total)
- Crash artifacts uploaded if any target fails

**Nightly (2 AM UTC)** (`.github/workflows/fuzz-nightly.yml`):
- Regression inputs replayed, then all 6 targets run for 30 minutes each
- Corpus cached and grows over time
- Any crash uploads artifacts (90-day retention) and opens a GitHub issue

## Regression Inputs

Every crash found by CI or locally must be captured as a permanent regression test:

```bash
# Minimize, then commit under the target's regressions directory
cargo +nightly fuzz tmin <target> fuzz/artifacts/<target>/crash-<hash>
cp fuzz/artifacts/<target>/minimized-from-<hash> fuzz/regressions/<target>/
```

Files in `fuzz/regressions/<target>/` are replayed once each (`-runs=0`) on every
PR and nightly run, so a fixed crash can never silently regress. Replay locally with:

```bash
cargo +nightly fuzz run <target> fuzz/regressions/<target> -- -runs=0
```

## Expected Panics

Some panics are **intentional** and should not be treated as bugs:

| Contract           | Function     | Panic Message           | Reason                    |
| ------------------ | ------------ | ----------------------- | ------------------------- |
| oracle_aggregator  | initialize   | "already initialized"   | Re-init protection        |
| zk_verifier        | initialize   | "already initialized"   | Re-init protection        |
| zk_verifier        | submit_score | "not initialized"       | Admin must be stored first |
| zk_verifier        | submit_score | (from require_auth)     | Stored-admin authorization |

The fuzz harnesses allow these via message matching.

## Troubleshooting

**"cargo: 'fuzz' is not a cargo command"**
```bash
cargo install cargo-fuzz
```

**"out of memory"**
```bash
cargo +nightly fuzz run <target> -- -max_len=512 -rss_limit_mb=2048
```

**"timeout: processing time exceeded"**
```bash
cargo +nightly fuzz run <target> -- -timeout=30
```

**Fuzzer finds no crashes but corpus is empty**
```bash
# Create corpus directory if missing
mkdir -p fuzz/corpus/<target>
```

## Full Documentation

See [docs/contract_fuzzing.md](../docs/contract_fuzzing.md) for:
- Architecture details
- Security considerations
- Corpus management
- Complete troubleshooting guide

## Contract Test Vectors

`tests/fixtures/contract_vectors.json` is generated from the canonical Python
models (`detection/risk_score.py`, `ingestion/data_models.py`) and consumed by
the Python, Rust, and TypeScript SDK contract tests. The `contract-vectors` job
in `.github/workflows/schema.yml` regenerates it on every PR and push to `main`
and **fails if the result differs from the committed file** — so a change to
contract behaviour cannot merge without regenerated vectors.

To intentionally update the vectors:

```bash
python scripts/generate_contract_vectors.py      # regenerate the fixture
git diff tests/fixtures/contract_vectors.json    # review what changed
python scripts/check_contract_vectors.py         # field-level drift report
pytest tests/test_contract_vectors.py            # Python round-trip tests
```

Then update any affected SDK (`sdk/src/schemas.ts`,
`crates/ledgerlens-sdk/src/models.rs`,
`packages/ledgerlens-sdk/src/ledgerlens/models.py`,
`proto/ledgerlens/v1/scoring.proto`) and commit the fixture in the same PR.
Never hand-edit the fixture; CI will reject anything the generator does not
reproduce exactly.
