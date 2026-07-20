"""Wire-format tests for `SorobanPublisher.submit_with_quorum`.

These assert the actual ScVal types sent to the Soroban host, which
`test_soroban_publisher.py` cannot do: that module replaces `stellar_sdk` with a
`MagicMock` at import time, so every ScVal it builds is a mock.

The distinction matters because the Python SDK does not validate Symbol
contents. `scval.to_symbol("XLM/USDC")` builds happily on the client and is only
rejected by the host at simulation time, so a client-side mock cannot catch an
encoding regression here.

The encoding must match `contracts/oracle_aggregator/src/lib.rs`:

    submit_with_quorum(
        wallet: Address,
        asset_pair: String,
        score: u32,
        timestamp: u64,
        signatures: Vec<SignaturePair>,
    )

where `SignaturePair` is a `#[contracttype]` struct, encoded as an SCV_MAP keyed
by field-name symbols rather than a positional vector.

Capture runs in a subprocess. `test_soroban_publisher.py` installs MagicMock
`stellar_sdk` entries into `sys.modules` at collection time, and re-importing
`detection.soroban_publisher` against the real SDK in-process leaves two
non-identical copies of the module — which breaks that module's
`patch("detection.soroban_publisher.SorobanServer", ...)` calls. A separate
interpreter sidesteps the shared-state problem entirely.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

WALLET = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
ASSET_PAIR = "XLM/USDC"
SCORE = 85
TIMESTAMP = 1672531200

# Runs in a clean interpreter: builds the invocation parameters exactly as
# `submit_with_quorum` does, then reports each one's ScVal type as JSON.
_CAPTURE_SCRIPT = f'''
import json, sys
from unittest.mock import MagicMock, patch

import stellar_sdk
from stellar_sdk import xdr as stellar_xdr

import detection.soroban_publisher as sp
from detection.oracle_coordinator import QuorumSignature

sp.save_submission = lambda *a, **k: None

server = MagicMock()
server.load_account.return_value = MagicMock(sequence=1)
sim = MagicMock(); sim.error = None; sim.min_resource_fee = "1000"
server.simulate_transaction.return_value = sim
send = MagicMock(); send.status = "PENDING"; send.hash = "ab" * 32; send.error = None
server.send_transaction.return_value = send
tx_res = MagicMock(); tx_res.status = "SUCCESS"; tx_res.error = None
server.get_transaction.return_value = tx_res

captured = {{}}

class _Builder:
    def __init__(self, *a, **k):
        pass
    def append_invoke_contract_function_op(self, contract_id, function_name, parameters):
        captured["function_name"] = function_name
        captured["parameters"] = parameters
        return self
    def build(self):
        return MagicMock()

publisher = sp.SorobanPublisher(
    contract_id="CA3CQ7C6YHK6K6C6J6C6K6C6K6C6K6C6K6C6K6C6K6C6K6C6K6C6K6C6",
    secret_key=stellar_sdk.Keypair.random().secret,
    soroban_rpc_url="https://soroban-testnet.stellar.org",
    network_passphrase="Test SDF Network ; September 2015",
)

sigs = [(bytes([i + 1] * 32).hex(), bytes([i + 100] * 64).hex()) for i in range(3)]
quorum = QuorumSignature(
    message_bytes=b"\\x00" * 32,
    signatures=sigs,
    signers_count=3,
    threshold=3,
    is_valid_quorum=True,
)

with patch.object(sp, "SorobanServer", return_value=server), \\
     patch.object(sp, "TransactionBuilder", _Builder), \\
     patch.object(publisher, "_keypair") as kp:
    kp.public_key = "{WALLET}"
    publisher.submit_with_quorum("{WALLET}", "{ASSET_PAIR}", {SCORE}, {TIMESTAMP}, quorum)

params = captured["parameters"]
T = stellar_xdr.SCValType

out = {{
    "function_name": captured["function_name"],
    "arity": len(params),
    "types": [p.type.name for p in params],
    "score": params[2].u32.uint32,
    "timestamp": params[3].u64.uint64,
    "asset_pair": (
        bytes(params[1].str.sc_string).decode()
        if params[1].type == T.SCV_STRING else None
    ),
    "signatures": [],
}}

for entry in params[4].vec.sc_vec:
    if entry.type != T.SCV_MAP:
        out["signatures"].append({{"type": entry.type.name}})
        continue
    keys = [bytes(e.key.sym.sc_symbol).decode() for e in entry.map.sc_map]
    vals = {{k: e.val for k, e in zip(keys, entry.map.sc_map)}}
    out["signatures"].append({{
        "type": entry.type.name,
        "keys": keys,
        "value_types": [vals[k].type.name for k in keys],
        "value_lens": [len(vals[k].bytes.sc_bytes) for k in keys],
    }})

print("__RESULT__" + json.dumps(out))
'''


@pytest.fixture(scope="module")
def encoded():
    """Invocation parameters as encoded by the real Stellar SDK."""
    pytest.importorskip("stellar_sdk")
    proc = subprocess.run(
        [sys.executable, "-c", _CAPTURE_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    marker = "__RESULT__"
    if proc.returncode != 0 or marker not in proc.stdout:
        pytest.fail(
            "encoding capture subprocess failed\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return json.loads(proc.stdout.split(marker, 1)[1].splitlines()[0])


def test_invokes_expected_contract_function(encoded):
    assert encoded["function_name"] == "submit_with_quorum"


def test_asset_pair_is_encoded_as_string_not_symbol(encoded):
    """`asset_pair` values contain '/' and ':', outside the Symbol charset, and
    can exceed its 32-character limit. A Symbol here is accepted client-side and
    rejected by the host, so pin the type."""
    assert encoded["types"][1] == "SCV_STRING", (
        "asset_pair must be an SCV_STRING; SCV_SYMBOL cannot represent 'XLM/USDC'"
    )


def test_asset_pair_roundtrips_with_special_characters(encoded):
    assert encoded["asset_pair"] == ASSET_PAIR


def test_signature_pairs_are_encoded_as_structs(encoded):
    """`SignaturePair` is a #[contracttype] struct -> SCV_MAP keyed by field
    name, not a positional SCV_VEC."""
    assert encoded["types"][4] == "SCV_VEC"
    sigs = encoded["signatures"]
    assert len(sigs) == 3

    for sig in sigs:
        assert sig["type"] == "SCV_MAP", (
            "each signature must be a struct-shaped SCV_MAP, not a positional vector"
        )
        assert sig["keys"] == ["public_key", "signature"], (
            "SignaturePair fields must be present and lexicographically ordered"
        )
        assert sig["value_types"] == ["SCV_BYTES", "SCV_BYTES"]
        assert sig["value_lens"] == [32, 64]


def test_scalar_params_match_contract_signature(encoded):
    """Positional arity and scalar types must line up with the Rust signature."""
    assert encoded["arity"] == 5
    assert encoded["types"][0] == "SCV_ADDRESS"
    assert encoded["types"][2] == "SCV_U32"
    assert encoded["types"][3] == "SCV_U64"
    assert encoded["score"] == SCORE
    assert encoded["timestamp"] == TIMESTAMP
