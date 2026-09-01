import numpy as np
import pytest
from detection.federated.homomorphic import HomomorphicAggregator

def make_client():
    return HomomorphicAggregator()

def test_round_trip():
    c = make_client()
    s = HomomorphicAggregator.from_public_context(c.public_context())
    p1 = np.linspace(0.1, 0.9, 64)
    p2 = np.linspace(0.9, 0.1, 64)
    enc = [c.encrypt_update(p1), c.encrypt_update(p2)]
    wt = [0.3, 0.7]
    out = c.decrypt_sum(s.homomorphic_sum(enc, wt))
    np.testing.assert_allclose(out, 0.3*p1+0.7*p2, atol=1e-2)

def test_no_secret_key_sum():
    c = make_client()
    s = HomomorphicAggregator.from_public_context(c.public_context())
    enc = c.encrypt_update(np.linspace(0, 1, 8))
    assert isinstance(s.homomorphic_sum([enc], [1.0]), bytes)
    with pytest.raises(Exception):
        s.decrypt_sum(enc)

def test_reject_secret_context():
    c = make_client()
    with pytest.raises(ValueError):
        HomomorphicAggregator.from_public_context(c._context.serialize())

def test_oversized_blob_rejected():
    c = make_client()
    s = HommorphicAggregator.from_public_context(c.public_context())
    old = HomomorphicAggregator.MAX_ENCRYPTED_BLOB_BYTES*