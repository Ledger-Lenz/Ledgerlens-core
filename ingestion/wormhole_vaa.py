"""Wormhole VAA parsing and guardian signature-set verification.

Bridge messages observed on Solana are only trusted once their VAA
(Verified Action Approval) carries a quorum of valid guardian signatures
from the *current* guardian set.  See ``docs/threat_model.md`` ("Cross-chain
bridge data trust model").

VAA v1 layout::

    version(1) guardian_set_index(4) num_signatures(1)
    signatures[num_signatures]: guardian_index(1) r(32) s(32) v(1)
    body: timestamp(4) nonce(4) emitter_chain(2) emitter_address(32)
          sequence(8) consistency_level(1) payload(...)

Each guardian signs ``keccak256(keccak256(body))`` with secp256k1; the
signer is identified by its 20-byte Ethereum-style address.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

VAA_VERSION = 1
_SIG_LEN = 66    # guardian_index(1) + r(32) + s(32) + v(1)
_BODY_MIN = 51   # timestamp..consistency_level


class VAAError(ValueError):
    """Raised when a VAA is malformed or fails verification."""


@dataclass(frozen=True)
class GuardianSignature:
    guardian_index: int
    r: int
    s: int
    v: int


@dataclass(frozen=True)
class ParsedVAA:
    version: int
    guardian_set_index: int
    signatures: tuple[GuardianSignature, ...]
    body: bytes
    timestamp: int
    nonce: int
    emitter_chain: int
    emitter_address: bytes
    sequence: int
    consistency_level: int
    payload: bytes


@dataclass(frozen=True)
class GuardianSet:
    """A Wormhole guardian set: its index and ordered 20-byte guardian addresses."""

    index: int
    addresses: tuple[bytes, ...]

    @classmethod
    def from_hex(cls, index: int, addresses: list[str]) -> GuardianSet:
        parsed = []
        for addr in addresses:
            raw = bytes.fromhex(addr.strip().removeprefix("0x"))
            if len(raw) != 20:
                raise ValueError(f"guardian address must be 20 bytes: {addr!r}")
            parsed.append(raw)
        return cls(index=index, addresses=tuple(parsed))

    @property
    def quorum(self) -> int:
        return len(self.addresses) * 2 // 3 + 1


def parse_vaa(raw: bytes) -> ParsedVAA:
    """Strictly parse a VAA v1 byte string; raises :class:`VAAError` on any defect."""
    if len(raw) < 6:
        raise VAAError(f"VAA too short for header ({len(raw)} bytes)")
    version = raw[0]
    if version != VAA_VERSION:
        raise VAAError(f"unsupported VAA version {version}")
    guardian_set_index = struct.unpack_from(">I", raw, 1)[0]
    num_sigs = raw[5]
    body_start = 6 + _SIG_LEN * num_sigs
    if len(raw) < body_start + _BODY_MIN:
        raise VAAError(
            f"VAA truncated: {len(raw)} bytes, need {body_start + _BODY_MIN} for {num_sigs} signature(s)"
        )
    sigs = []
    for i in range(num_sigs):
        off = 6 + _SIG_LEN * i
        sigs.append(GuardianSignature(
            guardian_index=raw[off],
            r=int.from_bytes(raw[off + 1:off + 33], "big"),
            s=int.from_bytes(raw[off + 33:off + 65], "big"),
            v=raw[off + 65],
        ))
    body = raw[body_start:]
    timestamp, nonce, emitter_chain = struct.unpack_from(">IIH", body, 0)
    sequence, consistency_level = struct.unpack_from(">QB", body, 42)
    return ParsedVAA(
        version=version,
        guardian_set_index=guardian_set_index,
        signatures=tuple(sigs),
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        emitter_chain=emitter_chain,
        emitter_address=body[10:42],
        sequence=sequence,
        consistency_level=consistency_level,
        payload=body[51:],
    )


def _keccak(data: bytes) -> bytes:
    from eth_utils import keccak

    return keccak(data)


def vaa_digest(body: bytes) -> bytes:
    """Digest guardians sign: ``keccak256(keccak256(body))``."""
    return _keccak(_keccak(body))


def recover_guardian_address(digest: bytes, sig: GuardianSignature) -> bytes:
    """Recover the 20-byte signer address of *sig* over *digest*."""
    from py_ecc.secp256k1 import ecdsa_raw_recover

    if sig.v not in (0, 1):
        raise VAAError(f"invalid signature recovery id {sig.v}")
    try:
        x, y = ecdsa_raw_recover(digest, (sig.v + 27, sig.r, sig.s))
    except Exception as exc:
        raise VAAError(f"signature recovery failed for guardian {sig.guardian_index}") from exc
    return _keccak(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:]


def verify_vaa(vaa: ParsedVAA, guardian_set: GuardianSet | None) -> None:
    """Verify *vaa* carries a quorum of valid signatures from *guardian_set*.

    Fails closed: raises :class:`VAAError` when no guardian set is configured,
    the VAA references another guardian set, signatures are out of order,
    duplicated, reference unknown guardians, do not recover to the expected
    guardian, or fall short of the ``2/3 + 1`` quorum.
    """
    if guardian_set is None or not guardian_set.addresses:
        raise VAAError("no guardian set configured; VAA cannot be verified")
    if vaa.guardian_set_index != guardian_set.index:
        raise VAAError(
            f"VAA signed by guardian set {vaa.guardian_set_index}, current is {guardian_set.index}"
        )
    if len(vaa.signatures) < guardian_set.quorum:
        raise VAAError(
            f"insufficient signatures: {len(vaa.signatures)} < quorum {guardian_set.quorum}"
        )
    digest = vaa_digest(vaa.body)
    last_index = -1
    for sig in vaa.signatures:
        if sig.guardian_index <= last_index:
            raise VAAError("guardian signatures not strictly ascending (duplicate or unordered)")
        last_index = sig.guardian_index
        if sig.guardian_index >= len(guardian_set.addresses):
            raise VAAError(f"unknown guardian index {sig.guardian_index}")
        if recover_guardian_address(digest, sig) != guardian_set.addresses[sig.guardian_index]:
            raise VAAError(f"invalid signature for guardian {sig.guardian_index}")


def guardian_set_from_settings() -> GuardianSet | None:
    """Build the current guardian set from ``WORMHOLE_GUARDIAN_SET_INDEX`` /
    ``WORMHOLE_GUARDIAN_ADDRESSES``; ``None`` when unconfigured."""
    from config.settings import settings

    addresses = [a for a in settings.wormhole_guardian_addresses.split(",") if a.strip()]
    if not addresses:
        return None
    return GuardianSet.from_hex(settings.wormhole_guardian_set_index, addresses)
