"""Atheris fuzz harness for the Asset Pydantic parser.

Target: Asset.model_validate(payload) — validates asset code/issuer fields
used as the base_asset / counter_asset in every Trade record.

The validator chain exercised:
  Asset.validate_string_fields (field_validator on code, issuer)
  Asset.validate_native_asset  (model_validator — non-XLM must have issuer)

Expected (safe) exceptions
--------------------------
pydantic.ValidationError — malformed input correctly rejected.
json.JSONDecodeError / UnicodeDecodeError — not Asset-shaped; skip.

Running locally:
    python fuzz/fuzz_asset_parser.py fuzz/corpus/fuzz_asset_parser -max_total_time=60

See fuzz/README.md for full local workflow.
"""

import sys

import atheris

with atheris.instrument_imports():
    import json

    from pydantic import ValidationError

    from ingestion.data_models import Asset


def TestOneInput(data: bytes) -> None:  # noqa: N802
    fdp = atheris.FuzzedDataProvider(data)
    raw = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return

    try:
        Asset.model_validate(payload)
    except ValidationError:
        pass


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
