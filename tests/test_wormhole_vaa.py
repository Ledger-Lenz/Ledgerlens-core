"""Wormhole VAA parsing / guardian signature verification (ingestion/wormhole_vaa.py)
and its enforcement in ingestion/solana_adapter.py."""
import base64
import struct

import pytest

pytest.importorskip("eth_utils")

from py_ecc.secp256k1 import ecdsa_raw_sign, privtopub

from ingestion.solana_adapter import WORMHOLE_CORE, _extract_stellar_address_from_vaa
from ingestion.wormhole_vaa import (
    GuardianSet,
    VAAError,
    _keccak,
    parse_vaa,
    vaa_digest,
    verify_vaa,
)

STELLAR_CHAIN_ID = 6
GUARDIAN_KEYS = [bytes([i + 1]) * 32 for i in range(4)]


def _address(priv: bytes) -> bytes:
    x, y = privtopub(priv)
    return _keccak(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:]


GUARDIAN_SET = GuardianSet(index=3, addresses=tuple(_address(k) for k in GUARDIAN_KEYS))


def _body(emitter: bytes, chain: int = STELLAR_CHAIN_ID) -> bytes:
    return struct.pack(">IIH", 1_700_000_000, 7, chain) + emitter + struct.pack(">QB", 42, 1) + b"payload"


def _vaa(body: bytes, signers: list[int], set_index: int = 3, keys=GUARDIAN_KEYS) -> bytes:
    digest = vaa_digest(body)
    out = struct.pack(">BIB", 1, set_index, len(signers))
    for idx in signers:
        v, r, s = ecdsa_raw_sign(digest, keys[idx])
        out += bytes([idx]) + r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v - 27])
    return out + body


def _tx(vaa: bytes) -> dict:
    data = base64.b64encode(b"\x02" + vaa).decode()
    return {"transaction": {"message": {
        "accountKeys": [WORMHOLE_CORE],
        "instructions": [{"programIdIndex": 0, "data": data}],
    }}}


EMITTER = bytes(range(32))


def test_valid_quorum_verifies():
    vaa = parse_vaa(_vaa(_body(EMITTER), [0, 1, 2]))
    assert vaa.emitter_chain == STELLAR_CHAIN_ID and vaa.emitter_address == EMITTER
    verify_vaa(vaa, GUARDIAN_SET)  # quorum for 4 guardians = 3


@pytest.mark.parametrize("signers,match", [
    ([0, 1], "insufficient"),
    ([0, 0, 1], "ascending"),
    ([2, 1, 0], "ascending"),
])
def test_signature_set_rules(signers, match):
    with pytest.raises(VAAError, match=match):
        verify_vaa(parse_vaa(_vaa(_body(EMITTER), signers)), GUARDIAN_SET)


def test_wrong_guardian_set_index_rejected():
    with pytest.raises(VAAError, match="guardian set"):
        verify_vaa(parse_vaa(_vaa(_body(EMITTER), [0, 1, 2], set_index=2)), GUARDIAN_SET)


def test_forged_signature_rejected():
    forged_keys = [bytes([0x99]) * 32] + GUARDIAN_KEYS[1:]
    with pytest.raises(VAAError, match="invalid signature"):
        verify_vaa(parse_vaa(_vaa(_body(EMITTER), [0, 1, 2], keys=forged_keys)), GUARDIAN_SET)


def test_tampered_body_rejected():
    raw = bytearray(_vaa(_body(EMITTER), [0, 1, 2]))
    raw[-1] ^= 0xFF
    with pytest.raises(VAAError):
        verify_vaa(parse_vaa(bytes(raw)), GUARDIAN_SET)


def test_no_guardian_set_fails_closed():
    with pytest.raises(VAAError, match="no guardian set"):
        verify_vaa(parse_vaa(_vaa(_body(EMITTER), [0, 1, 2])), None)


def test_adapter_accepts_verified_vaa():
    addr = _extract_stellar_address_from_vaa(_tx(_vaa(_body(EMITTER), [0, 1, 2])), guardian_set=GUARDIAN_SET)
    assert addr is not None and addr.startswith("G")


def test_adapter_rejects_and_reports_unverified_vaa():
    rejected = []
    addr = _extract_stellar_address_from_vaa(
        _tx(_vaa(_body(EMITTER), [0, 1])),
        guardian_set=GUARDIAN_SET,
        on_reject=lambda raw, reason: rejected.append(reason),
    )
    assert addr is None
    assert rejected and rejected[0].startswith("unverified")


def test_adapter_quarantines_rejected_vaa_in_dlq(tmp_path, monkeypatch):
    from detection.storage import init_db
    from ingestion import dlq as dlq_mod
    from ingestion.solana_adapter import SolanaAdapter

    db = str(tmp_path / "vaa.db")
    init_db(db)
    monkeypatch.setattr(dlq_mod.settings, "db_path", db)
    adapter = SolanaAdapter.__new__(SolanaAdapter)
    adapter._quarantine_vaa("sig1", b"\x01bad", "unverified: test")
    entries = dlq_mod.TradeDLQ(db_path=db).list_entries(status="quarantined")
    assert len(entries) == 1 and entries[0].source == "solana_wormhole_vaa"


# Malformed inputs of the kinds surfaced by fuzz/fuzz_solana_vaa_parser.py,
# kept as permanent regression cases.  None may raise anything but VAAError
# from parse_vaa, and none may yield a trusted Stellar address.
FUZZ_REGRESSION_INPUTS = [
    b"",
    b"\x01",
    b"\x01\x00\x00\x00\x03",                              # header cut before num_signatures
    b"\x01\x00\x00\x00\x03\xff" + b"\x00" * 60,           # num_signatures=255, data missing
    b"\x01\x00\x00\x00\x03\x01" + b"\x00" * 66 + b"\x00" * 26,  # body truncated mid-emitter
    b"\x02" + b"\x00" * 120,                              # unsupported version
    b"\x01\x00\x00\x00\x03\x00" + b"\x00" * 50,           # body one byte short
    _vaa(_body(EMITTER), [0])[:-60],                      # truncated signed VAA
]


@pytest.mark.parametrize("raw", FUZZ_REGRESSION_INPUTS)
def test_fuzz_regressions_parse_safely(raw):
    try:
        verify_vaa(parse_vaa(raw), GUARDIAN_SET)
    except VAAError:
        pass
    else:
        pytest.fail("malformed VAA verified")
    assert _extract_stellar_address_from_vaa(_tx(raw), guardian_set=GUARDIAN_SET) is None


@pytest.mark.parametrize("v", [2, 27, 255])
def test_fuzz_regression_bad_recovery_id(v):
    raw = bytearray(_vaa(_body(EMITTER), [0, 1, 2]))
    raw[6 + 65] = v
    with pytest.raises(VAAError):
        verify_vaa(parse_vaa(bytes(raw)), GUARDIAN_SET)


def test_fuzz_regression_invalid_curve_point():
    raw = bytearray(_vaa(_body(EMITTER), [0, 1, 2]))
    raw[7:39] = b"\x00" * 32  # r = 0
    with pytest.raises(VAAError):
        verify_vaa(parse_vaa(bytes(raw)), GUARDIAN_SET)
