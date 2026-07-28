# Fuzz Testing — LedgerLens Ingestion Parsers

This directory contains [Atheris](https://github.com/google/atheris) coverage-guided
fuzz harnesses for every Pydantic parser entrypoint in `ingestion/data_models.py` and
the byte-level parsers in `ingestion/solana_adapter.py`.

## Harnesses

| File | Target |
|------|--------|
| `fuzz_trade_parser.py` | `Trade.model_validate()` — Horizon trade JSON |
| `fuzz_asset_parser.py` | `Asset.model_validate()` — asset code/issuer fields |
| `fuzz_orderbook_event_parser.py` | `OrderBookEvent.model_validate()` — order-book ops |
| `fuzz_evm_rpc_parser.py` | `UniswapV3Adapter._parse_swap_event` + `CurveAdapter._parse_exchange_event` |
| `fuzz_solana_vaa_parser.py` | `_extract_stellar_address_from_vaa` + `_stellar_pubkey_to_address` + `_crc16_xmodem` |

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

The minimised file is written to `crash-<hash>-min`. Add the minimised bytes
as a regression fixture in `tests/test_data_models.py`.

## Corpus format

Each file in a corpus directory is a raw byte string that libFuzzer feeds to
`TestOneInput`. For JSON-layer harnesses the bytes represent a UTF-8 string
that is `json.loads`-parsed inside the harness; for the Solana VAA harness the
bytes are fed directly to the binary parsers.

**Do not seed the corpus from production data dumps** — crash corpus artifacts
may contain data resembling real wallet addresses. Use synthetically generated
or randomly mutated seeds only.

## Adding a new harness

1. Create `fuzz/fuzz_<target>.py` following the structure of an existing harness.
2. Catch only the exception types that are already handled as "expected" in the
   production ingestion code. Let everything else propagate as a finding.
3. Add the harness to the loop in `.github/workflows/nightly_fuzz.yml`.
4. Add a smoke-test case in `tests/test_fuzz_harness_smoke.py`.
5. Update this README's harness table.
