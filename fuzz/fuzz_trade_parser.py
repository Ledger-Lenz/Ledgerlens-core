"""Atheris fuzz harness for the Trade Pydantic parser.

Target: Trade.model_validate(payload) — the primary ingestion entrypoint
for Horizon trade JSON consumed by horizon_streamer.py and
historical_loader.py.

Expected (safe) exceptions
--------------------------
pydantic.ValidationError — malformed input correctly rejected by the schema.
json.JSONDecodeError / UnicodeDecodeError — not Trade-shaped; uninteresting.

Anything else that propagates (crash, hang, RecursionError, unexpected
TypeError) is treated as a genuine fuzzer finding and surfaces to libFuzzer
as a failing input.

Running locally (full fuzzing mode):
    python fuzz/fuzz_trade_parser.py fuzz/corpus/fuzz_trade_parser -max_total_time=60

Reproducing a CI crash artifact:
    python fuzz/fuzz_trade_parser.py fuzz/corpus/crash-<hash>

Minimising a crash:
    python fuzz/fuzz_trade_parser.py -minimize_crash=1 \
        -exact_artifact_path=fuzz/corpus/crash-<hash>-min \
        fuzz/corpus/crash-<hash>

See fuzz/README.md for full local workflow and corpus management.
"""

import sys

import atheris

with atheris.instrument_imports():
    import json

    from pydantic import ValidationError

    from ingestion.data_models import Trade


def TestOneInput(data: bytes) -> None:  # noqa: N802 — required by atheris convention
    fdp = atheris.FuzzedDataProvider(data)
    raw = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return  # not JSON; uninteresting to this harness

    try:
        Trade.model_validate(payload)
    except ValidationError:
        pass  # expected — malformed input correctly rejected by the schema
    # Anything else (crash, hang, RecursionError, unexpected TypeError) is a
    # genuine finding that atheris / libFuzzer will surface automatically.


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
