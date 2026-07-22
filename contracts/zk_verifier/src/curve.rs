//! BN254 (alt_bn128) field/curve arithmetic for Soroban, sized correctly for
//! the real 254-bit modulus and curve order (both represented as 256-bit
//! `U256` values -- the previous `u128` constants silently truncated a
//! 254-bit literal into a type that can hold at most 128 bits, which does
//! not even compile).
//!
//! # Two distinct modular integer types
//!
//! BN254 cryptography needs two different moduli, and conflating them (as
//! the previous implementation did, representing everything as bare
//! integers) is a correctness bug in itself:
//! - [`Fq`] -- a field element mod [`FIELD_MODULUS`] (`p`), used for point
//!   *coordinates* (x, y).
//! - [`Fr`] -- a scalar mod [`CURVE_ORDER`] (`n`), used for scalar
//!   multiplication exponents and the Sigma-protocol proof values
//!   (`c0`, `c1`, `s0`, `s1`, the Fiat-Shamir challenge). This matches
//!   `detection/zk_prover.py`, which reduces every one of these mod
//!   `curve_order` (`py_ecc.bn128.curve_order`), never mod the field prime.
//!
//! # Coordinate system: Jacobian, not affine
//!
//! [`Point`] is stored in Jacobian projective coordinates `(X, Y, Z)`
//! representing the affine point `(X/Z^2, Y/Z^3)`. This is not a style
//! preference -- it is required for the contract to fit any realistic gas
//! budget. `verify_threshold` (`lib.rs`) performs on the order of 10-20
//! scalar multiplications, each a ~254-bit double-and-add loop. In affine
//! coordinates, *every* point addition and doubling needs one field
//! inversion (an ~254-bit Fermat exponentiation, itself ~254 more
//! multiplications) -- roughly 14,000+ inversions, each costing hundreds of
//! multiplications, for a single `verify_threshold` call. In Jacobian
//! coordinates, addition and doubling need zero inversions; exactly one
//! inversion is needed per point, at the point where it is converted back
//! to affine for equality comparison or serialisation -- a reduction of
//! roughly two orders of magnitude. See `docs/zk_verifier_gas.md` for the
//! measured operation counts.
//!
//! # `mul_mod` reduction algorithm
//!
//! The previous `mul_mod` reduced a 256-bit intermediate product with a
//! `while remainder > 0 { ... remainder -= 1 }` loop -- for a remainder near
//! `2^128` this does not terminate in bounded time (or gas). This
//! implementation reduces the full (up to) 512-bit product via **binary
//! long division**: shift the modulus into alignment with the product's
//! highest set bit, then walk down one bit at a time, conditionally
//! subtracting -- a fixed, bounded 512 iterations regardless of input,
//! each a single compare-and-conditional-subtract. This is the
//! "schoolbook long-division reduction with a fixed iteration count"
//! option the issue explicitly sanctions, chosen over Montgomery/Barrett
//! reduction for auditability: every step is a direct, individually
//! checkable compare-subtract, cross-checked against 1,000+
//! Python-arbitrary-precision-arithmetic vectors
//! (`mod_arith_vectors.txt`, `src/test.rs`).

#![allow(clippy::many_single_char_names)]

use soroban_sdk::{Bytes, BytesN, Env};

// ---------------------------------------------------------------------------
// U256: fixed-width 256-bit unsigned integer, 4 little-endian u64 limbs
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct U256(pub [u64; 4]);

/// Big-integer ordering compares from the MOST significant limb (index 3)
/// down to the least significant (index 0) -- limbs are little-endian
/// (`self.0[0]` is the low 64 bits), so a naive derived `Ord` (which
/// compares element-wise starting from index 0) would silently produce
/// wrong results, e.g. treating `U256([5,0,0,0])` as greater than
/// `U256([3,1,0,0])` when the latter is actually ~2^64 times larger.
impl PartialOrd for U256 {
    fn partial_cmp(&self, other: &Self) -> Option<core::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for U256 {
    fn cmp(&self, other: &Self) -> core::cmp::Ordering {
        for i in (0..4).rev() {
            match self.0[i].cmp(&other.0[i]) {
                core::cmp::Ordering::Equal => continue,
                ord => return ord,
            }
        }
        core::cmp::Ordering::Equal
    }
}

impl U256 {
    pub const ZERO: U256 = U256([0, 0, 0, 0]);
    pub const ONE: U256 = U256([1, 0, 0, 0]);

    pub const fn from_u64(v: u64) -> Self {
        U256([v, 0, 0, 0])
    }

    pub fn is_zero(&self) -> bool {
        self.0 == [0, 0, 0, 0]
    }

    /// Parse a big-endian 32-byte array -- matches every integer encoding
    /// used elsewhere in this codebase (`int.to_bytes(32, "big")` on the
    /// Python side).
    pub fn from_be_bytes(b: &[u8; 32]) -> Self {
        let mut limbs = [0u64; 4];
        for i in 0..4 {
            // limb i covers bytes [24 - 8*i .. 32 - 8*i) (big-endian: limb 3 is most significant)
            let start = 24 - 8 * i;
            let mut v = 0u64;
            for j in 0..8 {
                v = (v << 8) | b[start + j] as u64;
            }
            limbs[i] = v;
        }
        U256(limbs)
    }

    pub fn to_be_bytes(&self) -> [u8; 32] {
        let mut out = [0u8; 32];
        for i in 0..4 {
            let start = 24 - 8 * i;
            let bytes = self.0[i].to_be_bytes();
            out[start..start + 8].copy_from_slice(&bytes);
        }
        out
    }

    /// Highest set bit index (0-based), or `None` if zero. Bounded at 256.
    fn bit_length(&self) -> u32 {
        for i in (0..4).rev() {
            if self.0[i] != 0 {
                return (i as u32) * 64 + (64 - self.0[i].leading_zeros());
            }
        }
        0
    }

    fn bit(&self, i: u32) -> bool {
        if i >= 256 {
            return false;
        }
        (self.0[(i / 64) as usize] >> (i % 64)) & 1 == 1
    }

    /// `self - other`, returning (result, borrow_out). Wraps on borrow.
    fn sub_borrow(&self, other: &U256) -> (U256, bool) {
        let mut out = [0u64; 4];
        let mut borrow = false;
        for i in 0..4 {
            let (d1, b1) = self.0[i].overflowing_sub(other.0[i]);
            let (d2, b2) = d1.overflowing_sub(borrow as u64);
            out[i] = d2;
            borrow = b1 || b2;
        }
        (U256(out), borrow)
    }

    /// `self + other`, returning (result, carry_out).
    fn add_carry(&self, other: &U256) -> (U256, bool) {
        let mut out = [0u64; 4];
        let mut carry = false;
        for i in 0..4 {
            let (s1, c1) = self.0[i].overflowing_add(other.0[i]);
            let (s2, c2) = s1.overflowing_add(carry as u64);
            out[i] = s2;
            carry = c1 || c2;
        }
        (U256(out), carry)
    }

    /// Full 256x256 -> 512-bit multiply. Returns 8 little-endian u64 limbs.
    fn mul_wide(&self, other: &U256) -> [u64; 8] {
        let mut acc = [0u128; 8];
        for i in 0..4 {
            if self.0[i] == 0 {
                continue;
            }
            let mut carry: u128 = 0;
            for j in 0..4 {
                let prod = (self.0[i] as u128) * (other.0[j] as u128) + acc[i + j] + carry;
                acc[i + j] = prod & 0xFFFF_FFFF_FFFF_FFFF;
                carry = prod >> 64;
            }
            // propagate remaining carry
            let mut k = i + 4;
            while carry != 0 {
                let s = acc[k] + carry;
                acc[k] = s & 0xFFFF_FFFF_FFFF_FFFF;
                carry = s >> 64;
                k += 1;
            }
        }
        let mut out = [0u64; 8];
        for i in 0..8 {
            out[i] = acc[i] as u64;
        }
        out
    }
}

/// A 512-bit value (as 8 little-endian u64 limbs) reduced modulo `m`
/// (a `U256`, i.e. at most 256 bits) via bounded binary long division:
/// walk the product's bits from the top down, maintaining a running
/// remainder and conditionally subtracting `m` -- 512 fixed iterations,
/// each O(1).
pub(crate) fn reduce_wide(x: [u64; 8], m: U256) -> U256 {
    let mut remainder = U256::ZERO;
    // Find the highest set bit across all 8 limbs (bounded scan, not data-dependent early-out).
    let mut top_bit: i32 = -1;
    for i in (0..8).rev() {
        if x[i] != 0 {
            top_bit = (i as i32) * 64 + (63 - x[i].leading_zeros() as i32);
            break;
        }
    }
    if top_bit < 0 {
        return U256::ZERO;
    }
    let mut i = top_bit;
    while i >= 0 {
        // remainder = remainder * 2 + bit(x, i)
        let bit = (x[(i / 64) as usize] >> (i % 64)) & 1;
        let (shifted, _overflow) = shl1_u256(&remainder);
        remainder = shifted;
        if bit == 1 {
            remainder.0[0] |= 1;
        }
        // If remainder >= m, subtract m (remainder stays < 2m before this, so one
        // conditional subtract per bit suffices -- standard binary long division).
        let (diff, borrow) = remainder.sub_borrow(&m);
        if !borrow {
            remainder = diff;
        }
        i -= 1;
    }
    remainder
}

fn shl1_u256(v: &U256) -> (U256, bool) {
    let mut out = [0u64; 4];
    let mut carry = 0u64;
    for i in 0..4 {
        out[i] = (v.0[i] << 1) | carry;
        carry = v.0[i] >> 63;
    }
    (U256(out), carry != 0)
}

// ---------------------------------------------------------------------------
// Modular arithmetic (generic over an arbitrary 256-bit modulus)
// ---------------------------------------------------------------------------

pub(crate) fn add_mod(a: U256, b: U256, m: U256) -> U256 {
    let (sum, carry) = a.add_carry(&b);
    if carry {
        // sum overflowed 256 bits; the true sum is >= 2^256 > m (since m < 2^256),
        // so subtracting m once always brings it back in range.
        let (diff, _) = sum.sub_borrow(&m);
        diff
    } else if sum >= m {
        let (diff, _) = sum.sub_borrow(&m);
        diff
    } else {
        sum
    }
}

pub(crate) fn sub_mod(a: U256, b: U256, m: U256) -> U256 {
    let (diff, borrow) = a.sub_borrow(&b);
    if borrow {
        let (fixed, _) = diff.add_carry(&m);
        fixed
    } else {
        diff
    }
}

pub(crate) fn mul_mod(a: U256, b: U256, m: U256) -> U256 {
    reduce_wide(a.mul_wide(&b), m)
}

/// `base^exp mod m` via square-and-multiply. `exp` is consumed bit by bit,
/// bounded at 256 iterations.
pub(crate) fn pow_mod(base: U256, exp: U256, m: U256) -> U256 {
    let mut result = U256::ONE;
    let mut b = base;
    let bits = exp.bit_length();
    for i in 0..bits {
        if exp.bit(i) {
            result = mul_mod(result, b, m);
        }
        b = mul_mod(b, b, m);
    }
    result
}

/// Modular inverse via Fermat's little theorem (`a^(m-2) mod m`) -- valid
/// because both `FIELD_MODULUS` and `CURVE_ORDER` are prime.
pub(crate) fn inv_mod(a: U256, m: U256) -> U256 {
    let two = U256::from_u64(2);
    let (m_minus_2, _) = m.sub_borrow(&two);
    pow_mod(a, m_minus_2, m)
}

// ---------------------------------------------------------------------------
// BN254 parameters
// ---------------------------------------------------------------------------

/// BN254 base field modulus `p` (254 bits).
/// `21888242871839275222246405745257275088696311157297823662689037894645226208583`
pub const FIELD_MODULUS: U256 = U256([
    0x3c208c16d87cfd47,
    0x97816a916871ca8d,
    0xb85045b68181585d,
    0x30644e72e131a029,
]);

/// BN254 scalar field / curve order `n` (254 bits).
/// `21888242871839275222246405745257275088548364400416034343698204186575808495617`
pub const CURVE_ORDER: U256 = U256([
    0x43e1f593f0000001,
    0x2833e84879b97091,
    0xb85045b68181585d,
    0x30644e72e131a029,
]);

// ---------------------------------------------------------------------------
// Fq: field element mod FIELD_MODULUS (point coordinates)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fq(pub U256);

impl Fq {
    pub fn zero() -> Self {
        Fq(U256::ZERO)
    }

    pub fn one() -> Self {
        Fq(U256::ONE)
    }

    pub fn from_u64(v: u64) -> Self {
        Fq(U256::from_u64(v) % FIELD_MODULUS)
    }

    /// Parse big-endian bytes, reducing mod `p` if the input is
    /// non-canonical (defensive: untrusted wire input should never be
    /// trusted to already be `< p`).
    pub fn from_be_bytes(b: &[u8; 32]) -> Self {
        Fq(U256::from_be_bytes(b) % FIELD_MODULUS)
    }

    pub fn from_bytesn(b: &BytesN<32>) -> Self {
        Self::from_be_bytes(&b.to_array())
    }

    pub fn to_be_bytes(&self) -> [u8; 32] {
        self.0.to_be_bytes()
    }

    pub fn is_zero(&self) -> bool {
        self.0.is_zero()
    }

    pub fn add(&self, other: &Fq) -> Fq {
        Fq(add_mod(self.0, other.0, FIELD_MODULUS))
    }

    pub fn sub(&self, other: &Fq) -> Fq {
        Fq(sub_mod(self.0, other.0, FIELD_MODULUS))
    }

    pub fn mul(&self, other: &Fq) -> Fq {
        Fq(mul_mod(self.0, other.0, FIELD_MODULUS))
    }

    pub fn neg(&self) -> Fq {
        if self.is_zero() {
            Fq::zero()
        } else {
            Fq(sub_mod(FIELD_MODULUS, self.0, FIELD_MODULUS))
        }
    }

    pub fn square(&self) -> Fq {
        self.mul(self)
    }

    pub fn invert(&self) -> Fq {
        Fq(inv_mod(self.0, FIELD_MODULUS))
    }

    pub fn is_valid(&self) -> bool {
        self.0 < FIELD_MODULUS
    }
}

impl core::ops::Rem<U256> for U256 {
    type Output = U256;
    fn rem(self, m: U256) -> U256 {
        if self < m {
            self
        } else {
            reduce_wide([self.0[0], self.0[1], self.0[2], self.0[3], 0, 0, 0, 0], m)
        }
    }
}

// ---------------------------------------------------------------------------
// Fr: scalar mod CURVE_ORDER (proof scalars c0/c1/s0/s1, exponents)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fr(pub U256);

impl Fr {
    pub fn from_u64(v: u64) -> Self {
        Fr(U256::from_u64(v) % CURVE_ORDER)
    }

    pub fn from_be_bytes(b: &[u8; 32]) -> Self {
        Fr(U256::from_be_bytes(b) % CURVE_ORDER)
    }

    pub fn from_bytesn(b: &BytesN<32>) -> Self {
        Self::from_be_bytes(&b.to_array())
    }

    pub fn add(&self, other: &Fr) -> Fr {
        Fr(add_mod(self.0, other.0, CURVE_ORDER))
    }

    pub fn eq(&self, other: &Fr) -> bool {
        self.0 == other.0
    }
}

// ---------------------------------------------------------------------------
// BN254 G1 point, Jacobian coordinates (X, Y, Z) ~ affine (X/Z^2, Y/Z^3)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug)]
pub struct Point {
    pub x: Fq,
    pub y: Fq,
    pub z: Fq,
}

impl Point {
    pub fn is_infinity(&self) -> bool {
        self.z.is_zero()
    }

    pub fn infinity() -> Self {
        Point { x: Fq::one(), y: Fq::one(), z: Fq::zero() }
    }

    /// The BN254 generator G1 = (1, 2) on y^2 = x^3 + 3.
    pub fn generator() -> Self {
        Point { x: Fq::one(), y: Fq::from_u64(2), z: Fq::one() }
    }

    /// Build an affine point from untrusted (wire) coordinates, rejecting
    /// anything not actually on the curve `y^2 = x^3 + 3`. Without this
    /// check, a forged proof could supply an (x, y) pair off the curve
    /// entirely -- the arithmetic below would still "run", but the result
    /// would not correspond to any real elliptic-curve point, which is
    /// exactly the kind of gap a soundness proof must not have.
    pub fn from_affine_checked(x: Fq, y: Fq) -> Option<Self> {
        let lhs = y.square();
        let rhs = x.square().mul(&x).add(&Fq::from_u64(3));
        if lhs == rhs {
            Some(Point { x, y, z: Fq::one() })
        } else {
            None
        }
    }

    /// "Nothing up my sleeve" second generator: `H = SHA256("LedgerLens ZK
    /// Generator H") mod n * G` -- computed at runtime via the real hash
    /// (matching `detection/zk_commitment.py::h_generator` exactly), not a
    /// hardcoded constant. The previous implementation hardcoded an
    /// unrelated scalar that did not correspond to this hash at all.
    pub fn h_generator(env: &Env) -> Self {
        let msg = Bytes::from_slice(env, b"LedgerLens ZK Generator H");
        let digest: BytesN<32> = env.crypto().sha256(&msg).into();
        let scalar = Fr::from_bytesn(&digest);
        Point::generator().mul_scalar(&scalar)
    }

    pub fn neg(&self) -> Self {
        if self.is_infinity() {
            *self
        } else {
            Point { x: self.x, y: self.y.neg(), z: self.z }
        }
    }

    /// Jacobian point doubling (a=0 curve), "dbl-2009-l" formula.
    pub fn double(&self) -> Point {
        if self.is_infinity() || self.y.is_zero() {
            return Point::infinity();
        }
        let a = self.x.square();
        let b = self.y.square();
        let c = b.square();
        let x1b = self.x.add(&b);
        let d = x1b.square().sub(&a).sub(&c);
        let d = d.add(&d);
        let e = a.add(&a).add(&a);
        let f = e.square();
        let x3 = f.sub(&d).sub(&d);
        let eight_c = c.add(&c).add(&c).add(&c);
        let eight_c = eight_c.add(&eight_c);
        let y3 = e.mul(&d.sub(&x3)).sub(&eight_c);
        let z3 = self.y.mul(&self.z);
        let z3 = z3.add(&z3);
        Point { x: x3, y: y3, z: z3 }
    }

    /// Jacobian point addition, "add-2007-bl" formula. Handles the
    /// infinity and doubling/negation special cases explicitly.
    pub fn add(&self, other: &Point) -> Point {
        if self.is_infinity() {
            return *other;
        }
        if other.is_infinity() {
            return *self;
        }
        let z1z1 = self.z.square();
        let z2z2 = other.z.square();
        let u1 = self.x.mul(&z2z2);
        let u2 = other.x.mul(&z1z1);
        let s1 = self.y.mul(&other.z).mul(&z2z2);
        let s2 = other.y.mul(&self.z).mul(&z1z1);
        let h = u2.sub(&u1);
        let r = s2.sub(&s1);
        if h.is_zero() {
            if r.is_zero() {
                return self.double();
            }
            return Point::infinity();
        }
        let i = h.add(&h).square();
        let j = h.mul(&i);
        let r2 = r.add(&r);
        let v = u1.mul(&i);
        let x3 = r2.square().sub(&j).sub(&v).sub(&v);
        let two_s1_j = s1.mul(&j).add(&s1.mul(&j));
        let y3 = r2.mul(&v.sub(&x3)).sub(&two_s1_j);
        let z1z2 = self.z.add(&other.z).square().sub(&z1z1).sub(&z2z2);
        let z3 = z1z2.mul(&h);
        Point { x: x3, y: y3, z: z3 }
    }

    /// Scalar multiplication via double-and-add, MSB to LSB, over the
    /// scalar's actual bit length (bounded at 254 bits for any `Fr`
    /// value, since `CURVE_ORDER < 2^254`).
    pub fn mul_scalar(&self, scalar: &Fr) -> Point {
        let mut result = Point::infinity();
        let bits = scalar.0.bit_length();
        let mut i = bits as i32 - 1;
        while i >= 0 {
            result = result.double();
            if scalar.0.bit(i as u32) {
                result = result.add(self);
            }
            i -= 1;
        }
        result
    }

    /// Convert to affine `(x, y)`. Exactly one field inversion -- callers
    /// should batch/minimize how many points need this (see module docs).
    pub fn to_affine(&self) -> (Fq, Fq) {
        if self.is_infinity() {
            return (Fq::zero(), Fq::zero());
        }
        let z_inv = self.z.invert();
        let z_inv2 = z_inv.square();
        let z_inv3 = z_inv2.mul(&z_inv);
        (self.x.mul(&z_inv2), self.y.mul(&z_inv3))
    }

    /// Equality via affine conversion. Two Jacobian representations of the
    /// same affine point are common (e.g. after independent computation
    /// chains), so raw `(X, Y, Z)` component comparison would be wrong.
    pub fn eq(&self, other: &Point) -> bool {
        if self.is_infinity() && other.is_infinity() {
            return true;
        }
        if self.is_infinity() != other.is_infinity() {
            return false;
        }
        // Cross-multiply to avoid two inversions: X1*Z2^2 == X2*Z1^2 and
        // Y1*Z2^3 == Y2*Z1^3.
        let z1z1 = self.z.square();
        let z2z2 = other.z.square();
        if self.x.mul(&z2z2) != other.x.mul(&z1z1) {
            return false;
        }
        let z1z1z1 = z1z1.mul(&self.z);
        let z2z2z2 = z2z2.mul(&other.z);
        self.y.mul(&z2z2z2) == other.y.mul(&z1z1z1)
    }

    /// Check if this field element is strictly less than the BN254 field modulus
    pub fn is_valid(&self) -> bool {
        const FIELD_MODULUS_LO: u128 = 201382436151624795304958197775988587847;
        const FIELD_MODULUS_HI: u128 = 64352033668853702584149021272023910493;
        if self.1 < FIELD_MODULUS_HI {
            return true;
        }
        if self.1 == FIELD_MODULUS_HI && self.0 < FIELD_MODULUS_LO {
            return true;
        }
        false
    }
}
