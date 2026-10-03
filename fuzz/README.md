# Fuzz Testing — LedgerLens Ingestion Parsers

This directory contains [Atheris](https://github.com/google/atheris) coverage-guided
fuzz harnesses for every Pydantic parser entrypoint in `ingestion/data_models.py` and
the byte-level parsers in `ingestion/solana_adapter.py`.

## Harnesses

| File | Target | Module | Run after changing |
|------|--------|--------|---------------------|
| `fuzz_trade_parser.py` | `Trade.model_validate()` — Horizon trade JSON | `ingestion/data_models.py` | `python fuzz/fuzz_trade_parser.py fuzz/corpus/fuzz_trade_parser -max_total_time=60` |
| `fuzz_asset_parser.py` | `Asset.model_validate()` — asset code/issuer fields | `ingestion/data_models.py` | `python fuzz/fuzz_asset_parser.py fuzz/corpus/fuzz_asset_parser -max_total_time=60` |
| `fuzz_orderbook_event_parser.py` | `OrderBookEvent.model_validate()` — order-book ops | `ingestion/data_models.py` | `python fuzz/fuzz_orderbook_event_parser.py fuzz/corpus/fuzz_orderbook_event_parser -max_total_time=60` |
| `fuzz_evm_rpc_parser.py` | `UniswapV3Adapter._parse_swap_event` + `CurveAdapter._parse_exchange_event` | `ingestion/uniswap_adapter.py`, `ingestion/curve_adapter.py` | `python fuzz/fuzz_evm_rpc_parser.py fuzz/corpus/fuzz_evm_rpc_parser -max_total_time=60` |
| `fuzz_solana_vaa_parser.py` | `_extract_stellar_address_from_vaa` + `_stellar_pubkey_to_address` + `_crc16_xmodem` | `ingestion/solana_adapter.py` | `python fuzz/fuzz_solana_vaa_parser.py fuzz/corpus/fuzz_solana_vaa_parser -max_total_time=60` |

## Continuous fuzzing

`.github/workflows/nightly_fuzz.yml` runs all five harnesses nightly (and on
manual dispatch) as a parallel matrix, 300s each:

1. **Fuzz** — each harness runs against its cached, growing corpus
   (`fuzz/corpus/<harness>/`) *plus* the committed regression corpus
   (`fuzz/regression/<harness>/`), so every known crash is replayed first.
   Findings are written to `fuzz/artifacts/<harness>/`.
2. **Track** — per-harness libFuzzer stats (execs, corpus size, crash count)
   are uploaded as `fuzz-results-<harness>` artifacts and summarised in the
   run's job summary, giving a run-over-run history.
3. **Triage** — `scripts/fuzz_triage.py` replays every crash, computes a
   signature (exception type + innermost in-repo frames) and keeps only the
   smallest input per signature. Signatures already in the regression corpus
   are reported as *known*, so one bug class never produces duplicates.
4. **Regress** — new unique reproducers are committed to
   `fuzz/regression/<harness>/<signature>.bin` via an automatically opened PR.
   `tests/test_fuzz_regression.py` replays that corpus on every CI run.

(`fuzz-nightly.yml` is a separate workflow for the Rust contract fuzzers.)

## Prerequisites

```bash
pip install atheris
```

Atheris requires a Python build linked against libFuzzer (standard on Linux
with CPython from `apt` or `pyenv`). On macOS, use the Homebrew LLVM-linked
Python or a Docker container.

## Running a harness locally

Each harness is a standalone script. Pass a corpus directory as the first
positional argument and `-max_total_time=<seconds>` to bound the run:

```bash
# Run the Trade parser fuzzer for 60 seconds, using/building a local corpus:
python fuzz/fuzz_trade_parser.py fuzz/corpus/fuzz_trade_parser -max_total_time=60

# Run the Solana VAA byte parser for 60 seconds:
python fuzz/fuzz_solana_vaa_parser.py fuzz/corpus/fuzz_solana_vaa_parser -max_total_time=60
```

If the corpus directory does not exist it is created automatically by libFuzzer.
Interesting inputs discovered during the run are written into the corpus directory
so subsequent runs benefit from them.

## Quick smoke check (no Atheris required)

The `fuzz-quick` Makefile target (and `python cli.py fuzz-check`) runs each
harness for 30 seconds using Atheris's built-in iteration cap. This is suitable
for pre-merge validation:

```bash
make fuzz-quick
# or
python cli.py fuzz-check
```

## Reproducing a CI crash artifact

When the nightly CI job (`nightly_fuzz.yml`) detects a crash it uploads the
crash input as a build artifact named `fuzz-crashes`. Download the artifact,
then pass the crash file directly to the harness:

```bash
python fuzz/fuzz_trade_parser.py fuzz/corpus/crash-<hash>
```

The harness will re-run `TestOneInput` on that exact byte sequence and
reproduce the crash.

## Minimising a crash input

libFuzzer's built-in minimiser shrinks a crash to its smallest reproducing form:

```bash
python fuzz/fuzz_trade_parser.py \
    -minimize_crash=1 \
    -exact_artifact_path=fuzz/corpus/crash-<hash>-min \
    fuzz/corpus/crash-<hash>
```

The minimised file is written to `crash-<hash>-min`. Copy it into
`fuzz/regression/<harness>/` (the nightly triage job does this automatically)
so it is replayed on every CI run.

## Corpus format

Each file in a corpus directory is a raw byte string that libFuzzer feeds to
`TestOneInput`. For JSON-layer harnesses the bytes represent a UTF-8 string
that is `json.loads`-parsed inside the harness; for the Solana VAA harness the
bytes are fed directly to the binary parsers.

**Do not seed the corpus from production data dumps** — crash corpus artifacts
may contain data resembling real wallet addresses. Use synthetically generated
or randomly mutated seeds only.

## Adding a new fuzz target

1. Create `fuzz/fuzz_<target>.py` following the structure of an existing harness.
2. Catch only the exception types that are already handled as "expected" in the
   production ingestion code. Let everything else propagate as a finding.
3. Add `fuzz_<target>` to the `matrix.harness` list in
   `.github/workflows/nightly_fuzz.yml`.
4. Create `fuzz/regression/fuzz_<target>/.gitkeep` (the regression corpus dir).
5. Add an Atheris-free `_call_<target>` mirror and smoke cases in
   `tests/test_fuzz_harness_smoke.py`, and register it in `_CALLERS` in
   `tests/test_fuzz_regression.py` (a test fails until you do).
6. Update this README's harness table.
