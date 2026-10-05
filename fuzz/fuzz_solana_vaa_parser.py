"""Atheris fuzz harness for the Solana Wormhole VAA byte-level parser.

Targets the byte-level parsing functions in ingestion/solana_adapter.py and
ingestion/wormhole_vaa.py:
  - _extract_stellar_address_from_vaa(tx_dict)
  - parse_vaa(raw) / verify_vaa(vaa, guardian_set)
  - _stellar_pubkey_to_address(raw_key: bytes)
  - _crc16_xmodem(data: bytes)

These operate on raw bytes rather than JSON, making them prime targets for
coverage-guided fuzzing. The VAA parser in particular has complex branching
on byte offsets (guardian signature block, emitter_chain, emitter address)
that a Hypothesis strategy is unlikely to exhaustively probe.

Two sub-harnesses are exercised per input:

1. **VAA transaction harness** — wraps the full _extract_stellar_address_from_vaa
   call with a synthetic transaction dict whose instruction data field is the
   raw fuzz bytes (base64-encoded, as Horizon/Solana RPC would return).

2. **Raw pubkey harness** — calls _stellar_pubkey_to_address directly on the
   first 32 bytes of input, exercising _crc16_xmodem and base32-check encoding.

Expected (safe) exceptions
--------------------------
ValueError, KeyError, IndexError, struct.error — malformed byte layouts.
(VAAError subclasses ValueError.)

Findings are folded in as permanent regression tests in
tests/test_wormhole_vaa.py (``FUZZ_REGRESSION_INPUTS``).

Running locally (byte-level fuzzing, no JSON layer):
    python fuzz/fuzz_solana_vaa_parser.py fuzz/corpus/fuzz_solana_vaa_parser -max_total_time=60

Reproducing a CI crash artifact:
    python fuzz/fuzz_solana_vaa_parser.py fuzz/corpus/crash-<hash>

See fuzz/README.md for full local workflow.
"""

import base64
import struct
import sys

import atheris

with atheris.instrument_imports():
    from ingestion.solana_adapter import (
        WORMHOLE_CORE,
        _crc16_xmodem,
        _extract_stellar_address_from_vaa,
        _stellar_pubkey_to_address,
    )
    from ingestion.wormhole_vaa import GuardianSet, parse_vaa, verify_vaa

# Fixed single-guardian set so verification paths are exercised; fuzz inputs
# must never verify against it.
_GUARDIAN_SET = GuardianSet(index=0, addresses=(b"\x11" * 20,))


def _make_tx_dict(raw_bytes: bytes) -> dict:
    """Wrap raw bytes in a minimal Solana transaction dict.

    The data field is base64-encoded as the Solana RPC would return it,
    and the programIdIndex references the Wormhole core bridge so the
    parser actually enters the VAA parsing branch.
    """
    return {
        "transaction": {
            "message": {
                "accountKeys": [WORMHOLE_CORE],
                "instructions": [
                    {
                        "programIdIndex": 0,
                        "data": base64.b64encode(raw_bytes).decode("ascii"),
                    }
                ],
            }
        }
    }


def TestOneInput(data: bytes) -> None:  # noqa: N802
    fdp = atheris.FuzzedDataProvider(data)

    # Sub-harness 1: full VAA transaction parser.
    vaa_bytes = fdp.ConsumeBytes(min(fdp.remaining_bytes(), 256))
    tx = _make_tx_dict(vaa_bytes)
    try:
        _extract_stellar_address_from_vaa(tx, guardian_set=_GUARDIAN_SET)
    except (ValueError, KeyError, IndexError, struct.error):
        pass
    try:
        verify_vaa(parse_vaa(vaa_bytes), _GUARDIAN_SET)
        raise AssertionError("fuzz input verified against a fixed guardian set")
    except ValueError:
        pass

    # Sub-harness 2: raw pubkey → Stellar address encoder + CRC.
    if fdp.remaining_bytes() >= 1:
        key_bytes = fdp.ConsumeBytes(fdp.remaining_bytes())
        try:
            _stellar_pubkey_to_address(key_bytes)
        except (ValueError, struct.error):
            pass
        try:
            _crc16_xmodem(key_bytes)
        except Exception:
            pass


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
