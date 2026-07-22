"""Atheris fuzz harness for the EVM JSON-RPC response parser.

Target: UniswapV3Adapter._parse_swap_event and CurveAdapter._parse_exchange_event
— parses raw eth_getLogs responses from EVM JSON-RPC endpoints. Exercises:

  - hex string decoding of ``data`` and ``topics`` fields
  - ABI-decoding of Uniswap V3 Swap / Curve TokenExchange event payloads
  - Address checksum validation via web3.py's to_checksum_address
  - int conversion of block numbers and amounts

The harness feeds arbitrary JSON directly to the log-parsing methods,
bypassing the HTTP layer entirely so the fuzzer never makes real network calls.

Expected (safe) exceptions
--------------------------
ValueError, KeyError, IndexError, TypeError — malformed RPC response fields.
pydantic.ValidationError — downstream model construction rejected input.

Running locally:
    python fuzz/fuzz_evm_rpc_parser.py fuzz/corpus/fuzz_evm_rpc_parser -max_total_time=60

See fuzz/README.md for full local workflow.
"""

import sys

import atheris

with atheris.instrument_imports():
    import json

    from pydantic import ValidationError

    from ingestion.uniswap_adapter import UniswapV3Adapter
    from ingestion.curve_adapter import CurveAdapter


# Minimal stub adapters — only the log-parsing methods are called; no HTTP.
_uniswap = UniswapV3Adapter.__new__(UniswapV3Adapter)
_uniswap._rpc_url = "https://stub.invalid"
_uniswap._chain = "ethereum"
_uniswap._linked_wallets = set()

_curve = CurveAdapter.__new__(CurveAdapter)
_curve._rpc_url = "https://stub.invalid"
_curve._chain = "ethereum"
_curve._linked_wallets = set()


def TestOneInput(data: bytes) -> None:  # noqa: N802
    fdp = atheris.FuzzedDataProvider(data)
    raw = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return

    if not isinstance(payload, dict):
        return

    try:
        _uniswap._parse_swap_event(payload)
    except (ValueError, KeyError, IndexError, TypeError, ValidationError):
        pass

    try:
        _curve._parse_exchange_event(payload)
    except (ValueError, KeyError, IndexError, TypeError, ValidationError):
        pass


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
