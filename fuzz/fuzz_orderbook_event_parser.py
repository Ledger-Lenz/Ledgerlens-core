"""Atheris fuzz harness for the OrderBookEvent Pydantic parser.

Target: OrderBookEvent.model_validate(payload) — parses order-placement,
update, and cancellation events from Horizon operations ingested by
ingestion/operations_loader.py.

Validators exercised:
  OrderBookEvent.parse_numeric_fields (amount, price sanitisation)
  Field constraints: side ∈ {"buy","sell"}, event_type ∈ {"created","updated","cancelled"}
  offer_id PositiveInteger constraint (zero is rejected)

Expected (safe) exceptions
--------------------------
pydantic.ValidationError — malformed input correctly rejected.
json.JSONDecodeError / UnicodeDecodeError — skip.

Running locally:
    python fuzz/fuzz_orderbook_event_parser.py \
        fuzz/corpus/fuzz_orderbook_event_parser -max_total_time=60

See fuzz/README.md for full local workflow.
"""

import sys

import atheris

with atheris.instrument_imports():
    import json

    from pydantic import ValidationError

    from ingestion.data_models import OrderBookEvent


def TestOneInput(data: bytes) -> None:  # noqa: N802
    fdp = atheris.FuzzedDataProvider(data)
    raw = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return

    try:
        OrderBookEvent.model_validate(payload)
    except ValidationError:
        pass


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
