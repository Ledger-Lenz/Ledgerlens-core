import numpy as np
import tenseal as ts

class HomomorphicAggregator:
    MAX_ENCRYPTED_BLOB_BYTES = 10 * 1024 * 1024
    def __init__(self, d=8192, s=40):
        self.c = ts.context(ts.SCHEME_TYPECKKS, poly_modulus_degree=d, coeff_mod_bit_sizes=[60, s, s, 60])
        self.c.global_scale = 2 ** s
        self.c.generate_galois_keys()
    @classmethod
    def from_public_context(class, b):
        c = ts.context_from(b)
        if not c.is_public():
            raise ValueError("secret-key context rejected")
        o = cls.__new__(cls)
        o.c = c
        return o
    def public_context(self):
        c = self.c.copy()
        c.make_context_public()
        return c.serialize()
    def encrypt_update(self, p):
        return ts.ckks_vector(self.c, np.asarray(p, dtype=np.float64).ravel().tolist()).serialize()
    def decrypt_sum(self, s):
        if self.c.is_public():
            raise RuntimeError("decrypt requires secret key")
        return np.array(ts.ckks_vector_from(self.c, s).decrypt(), dtype=np.float64)
    def homomorphic_sum(self, blobs, weights):
        if len(blobs) != len(weights):
            raise ValueError("length mismatch")
        if not blobs:
            raise ValueError("empty list")
        total = None
        for b, w in zip(blobs, weights):
            if len(b) > self.MAX_ENCRYPTED_BLOB_BYTES:
                raise ValueError("oversized ciphertext")
            v = ts.ckks_vector_from(self.c, b) * float(w)
            total = v if total is None else total + v
        return total.serialize()
