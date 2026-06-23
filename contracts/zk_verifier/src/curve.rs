//! Minimal BN254 (alt_bn128) curve arithmetic for Soroban.
//!
//! Only the operations needed for Sigma-protocol verification:
//! point addition, negation, scalar multiplication, and equality.

use soroban_sdk::env;

/// BN254 field modulus.
const FIELD_MODULUS: u128 = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

// We store field elements as u256 (two u128 limbs).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fq(pub u128, pub u128);

/// BN254 curve order (for scalar multiplication).
const CURVE_ORDER: u128 = 21888242871839275222246405745257275088548364400416034343698204186575808495617;

/// Trait to convert small integers to field elements.
impl From<u64> for Fq {
    fn from(v: u64) -> Self {
        Fq(v as u128, 0)
    }
}

// ---------------------------------------------------------------------------
// Modular arithmetic helpers
// ---------------------------------------------------------------------------

const fn add_mod(a: u128, b: u128, m: u128) -> u128 {
    let (sum, carry) = a.overflowing_add(b);
    if carry || sum >= m { sum.wrapping_sub(m) } else { sum }
}

fn sub_mod(a: u128, b: u128, m: u128) -> u128 {
    if a >= b { a - b } else { m - (b - a) }
}

fn mul_mod(a: u128, b: u128, m: u128) -> u128 {
    // a * b mod m using 128-bit arithmetic.
    // Since a, b < m < 2^128, a*b can be up to 2^256.
    // We use the schoolbook method with splitting.
    let a0 = a & 0xFFFFFFFFFFFFFFFF;
    let a1 = a >> 64;
    let b0 = b & 0xFFFFFFFFFFFFFFFF;
    let b1 = b >> 64;

    // Compute partial products
    let p00 = a0 as u128 * b0 as u128;
    let p01 = a0 as u128 * b1 as u128;
    let p10 = a1 as u128 * b0 as u128;
    let p11 = a1 as u128 * b1 as u128;

    // Sum with carries (using 128-bit arithmetic, tracking overflow)
    let (low, c1) = p00.overflowing_add((p01 & 0xFFFFFFFFFFFFFFFF) << 64);
    let (low, c2) = low.overflowing_add((p10 & 0xFFFFFFFFFFFFFFFF) << 64);
    let mut high = p11 + (p01 >> 64) + (p10 >> 64) + (c1 as u128) + (c2 as u128);

    // Now high:low is the 256-bit product. Reduce mod m using Barrett-like approach.
    // Since m is close to 2^128, we can use a simple reduction.
    // This is a simplified version; for a production contract use a proper reduction.
    let mut result = low;
    let mut remainder = high;
    while remainder > 0 {
        let (tmp, overflow) = result.overflowing_sub(m - (remainder % m));
        result = tmp;
        if overflow {
            result = result.wrapping_add(m);
        }
        remainder -= 1;
    }
    if result >= m {
        result -= m;
    }
    result
}

// ---------------------------------------------------------------------------
// Field element arithmetic
// ---------------------------------------------------------------------------

impl Fq {
    pub fn zero() -> Self {
        Fq(0, 0)
    }

    pub fn one() -> Self {
        Fq(1, 0)
    }

    pub fn is_zero(&self) -> bool {
        self.0 == 0 && self.1 == 0
    }

    /// Negation in the field.
    pub fn neg(&self) -> Self {
        if self.is_zero() {
            return Fq::zero();
        }
        // Compute m - self
        let modulus_words = [FIELD_MODULUS, 0];
        let self_words = [self.0, self.1];
        let (lo, borrow) = modulus_words[0].overflowing_sub(self_words[0]);
        let hi = modulus_words[1].wrapping_sub(self_words[1]).wrapping_sub(borrow as u128);
        Fq(lo, hi)
    }

    pub fn add(&self, other: &Fq) -> Self {
        let (lo, carry) = self.0.overflowing_add(other.0);
        let hi = self.1.wrapping_add(other.1).wrapping_add(carry as u128);
        // Reduce if >= modulus
        if hi > 0 || lo >= FIELD_MODULUS {
            let (lo2, borrow2) = lo.overflowing_sub(FIELD_MODULUS);
            let hi2 = hi.wrapping_sub(borrow2 as u128);
            Fq(lo2, hi2)
        } else {
            Fq(lo, hi)
        }
    }

    pub fn sub(&self, other: &Fq) -> Self {
        let (lo, borrow) = self.0.overflowing_sub(other.0);
        let hi = self.1.wrapping_sub(other.1).wrapping_sub(borrow as u128);
        // If negative (hi bit 127 set means negative in two's complement)
        if hi >> 127 == 1 {
            let (lo2, carry) = lo.overflowing_add(FIELD_MODULUS);
            let hi2 = hi.wrapping_add(carry as u128);
            Fq(lo2, hi2 & 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF)
        } else {
            Fq(lo, hi)
        }
    }

    pub fn mul(&self, other: &Fq) -> Self {
        // Full 256-bit multiplication reduced mod FIELD_MODULUS
        // Split each operand into 4 64-bit limbs
        let a = [self.0 as u64, (self.0 >> 64) as u64, self.1 as u64, (self.1 >> 64) as u64];
        let b = [other.0 as u64, (other.0 >> 64) as u64, other.1 as u64, (other.1 >> 64) as u64];

        // Schoolbook multiplication into 8 limbs
        let mut p = [0u64; 8];
        for i in 0..4 {
            for j in 0..4 {
                let (lo, hi) = mul64_128(a[i], b[j]);
                let mut k = i + j;
                // Add lo
                let (sum, carry) = p[k].overflowing_add(lo as u64);
                p[k] = sum;
                let mut c = carry as u64;
                k += 1;
                // Add hi plus carry
                let (sum2, carry2) = p[k].overflowing_add(hi as u64);
                p[k] = sum2;
                c = c.wrapping_add(carry2 as u64);
                k += 1;
                while c > 0 && k < 8 {
                    let (sum3, carry3) = p[k].overflowing_add(c);
                    p[k] = sum3;
                    c = carry3 as u64;
                    k += 1;
                }
            }
        }

        // Convert to two u128
        let lo = (p[0] as u128) | ((p[1] as u128) << 64);
        let hi = (p[2] as u128) | ((p[3] as u128) << 64);
        let hi2 = (p[4] as u128) | ((p[5] as u128) << 64);
        let hi3 = (p[6] as u128) | ((p[7] as u128) << 64);

        // Reduce: product = hi3*2^192 + hi2*2^128 + hi*2^64 + lo
        // Since FIELD_MODULUS < 2^128, we can use Montgomery-like reduction
        // For simplicity, just do repeated subtraction
        let mut result_lo = lo;
        let mut result_hi = hi;
        let mut extra = [hi2, hi3];

        // Reduction loop: while extra > 0, subtract modulus * (extra % modulus)
        // This is a simple but not constant-time reduction.
        while extra[0] != 0 || extra[1] != 0 {
            // Estimate quotient
            let q = if extra[1] > 0 { extra[0] } else { extra[0] };
            let q = q % FIELD_MODULUS;
            if q == 0 {
                // Subtract FIELD_MODULUS from extra
                let (le, be) = extra[0].overflowing_sub(1);
                extra[0] = le;
                if be {
                    extra[1] = extra[1].wrapping_sub(1);
                }
                continue;
            }
            // Subtract q * FIELD_MODULUS from result
            let q_mod = q; // already reduced
            let (prod_lo, prod_hi) = mul128(q_mod, FIELD_MODULUS);
            let (new_lo, borrow1) = result_lo.overflowing_sub(prod_lo);
            result_lo = new_lo;
            let new_hi = result_hi.wrapping_sub(prod_hi).wrapping_sub(borrow1 as u128);
            result_hi = new_hi;
            // Carry if hi went negative
            if result_hi >> 127 == 1 {
                let (rl, carry) = result_lo.overflowing_add(FIELD_MODULUS);
                result_lo = rl;
                result_hi = result_hi.wrapping_add(carry as u128);
                // Need to borrow from extra
                let (le, be) = extra[0].overflowing_sub(1);
                extra[0] = le;
                if be {
                    extra[1] = extra[1].wrapping_sub(1);
                }
            }
            extra[0] = extra[0].wrapping_sub(q);
            if extra[0] > 0xFFFFFFFFFFFFFFFF - q {
                extra[1] = extra[1].wrapping_sub(1);
            }
        }

        // Final reduction
        let mut r = Fq(result_lo, result_hi);
        if r.1 > 0 || r.0 >= FIELD_MODULUS {
            let (le, bo) = r.0.overflowing_sub(FIELD_MODULUS);
            r.0 = le;
            r.1 = r.1.wrapping_sub(bo as u128);
        }
        r
    }
}

fn mul64_128(a: u64, b: u64) -> (u128, u128) {
    let prod = a as u128 * b as u128;
    (prod & 0xFFFFFFFFFFFFFFFF, prod >> 64)
}

fn mul128(a: u128, b: u128) -> (u128, u128) {
    let a_lo = a as u64;
    let a_hi = (a >> 64) as u64;
    let b_lo = b as u64;
    let b_hi = (b >> 64) as u64;

    let p00 = a_lo as u128 * b_lo as u128;
    let p01 = a_lo as u128 * b_hi as u128;
    let p10 = a_hi as u128 * b_lo as u128;
    let p11 = a_hi as u128 * b_hi as u128;

    let (low, c1) = p00.overflowing_add((p01 & 0xFFFFFFFFFFFFFFFF) << 64);
    let (low, c2) = low.overflowing_add((p10 & 0xFFFFFFFFFFFFFFFF) << 64);

    let high = p11 + (p01 >> 64) + (p10 >> 64) + (c1 as u128) + (c2 as u128);
    (low, high & 0xFFFFFFFFFFFFFFFF)
}

// ---------------------------------------------------------------------------
// BN254 point (affine coordinates)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Point {
    pub x: Fq,
    pub y: Fq,
    pub infinity: bool,
}

impl Point {
    /// The BN254 generator G1 = (1, 2) on y^2 = x^3 + 3.
    pub fn generator() -> Self {
        Point {
            x: Fq::one(),
            y: Fq(2, 0),
            infinity: false,
        }
    }

    /// Nothing-up-my-sleeve second generator H = SHA256("LedgerLens ZK Generator H") * G.
    pub fn h_generator() -> Self {
        let scalar = [
            0x4815FA6Du64, 0xFE6D7D8Bu64, 0x9315D3E1u64, 0x5E20EEF6u64,
        ];
        let mut p = Point::generator();
        for &limb in scalar.iter().rev() {
            p = p.double();
            if limb & 1 == 1 {
                p = p.add(&Point::generator());
            }
        }
        p
    }

    pub fn zero() -> Self {
        Point {
            x: Fq::zero(),
            y: Fq::zero(),
            infinity: true,
        }
    }

    pub fn is_zero(&self) -> bool {
        self.infinity
    }

    /// Negate point: (x, y) -> (x, -y).
    pub fn neg(&self) -> Self {
        if self.infinity {
            return *self;
        }
        Point {
            x: self.x,
            y: self.y.neg(),
            infinity: false,
        }
    }

    /// Affine point addition (complete formula for BN254, a=0).
    pub fn add(&self, other: &Point) -> Point {
        if self.infinity {
            return *other;
        }
        if other.infinity {
            return *self;
        }

        // Check if points are negations of each other
        if self.x == other.x {
            if self.y == other.y.neg() {
                return Point::zero();
            }
            // Point doubling
            return self.double_internal();
        }

        let lambda = other.y.sub(&self.y).mul(&other.x.sub(&self.x).invert());
        let x3 = lambda.mul(&lambda).sub(&self.x).sub(&other.x);
        let y3 = lambda.mul(&self.x.sub(&x3)).sub(&self.y);
        Point { x: x3, y: y3, infinity: false }
    }

    fn double_internal(&self) -> Point {
        if self.infinity || self.y.is_zero() {
            return Point::zero();
        }
        // λ = (3 * x^2) / (2 * y)  for a=0
        let three = Fq(3, 0);
        let two = Fq(2, 0);
        let num = three.mul(&self.x.mul(&self.x));
        let den = two.mul(&self.y);
        let lambda = num.mul(&den.invert());
        let x3 = lambda.mul(&lambda).sub(&self.x).sub(&self.x);
        let y3 = lambda.mul(&self.x.sub(&x3)).sub(&self.y);
        Point { x: x3, y: y3, infinity: false }
    }

    /// Double using the internal method.
    pub fn double(&self) -> Point {
        self.double_internal()
    }

    /// Scalar multiplication using double-and-add.
    pub fn mul_scalar(&self, scalar: &Fq) -> Point {
        let mut result = Point::zero();
        let mut base = *self;
        // We need to iterate through bits of the scalar.
        // Since scalar is a field element (two u128 limbs), we iterate 254 bits.
        let limbs = [scalar.0, scalar.1];
        for limb in limbs.iter() {
            for bit in 0..128 {
                if (*limb >> bit) & 1 == 1 {
                    result = result.add(&base);
                }
                base = base.double();
            }
        }
        result
    }

    /// Check equality of two points.
    pub fn eq(&self, other: &Point) -> bool {
        if self.infinity && other.infinity {
            return true;
        }
        if self.infinity != other.infinity {
            return false;
        }
        self.x == other.x && self.y == other.y
    }
}

impl Fq {
    /// Compute modular inverse using Fermat's little theorem: a^(m-2) mod m.
    pub fn invert(&self) -> Self {
        if self.is_zero() {
            return Fq::zero();
        }
        // exponent = FIELD_MODULUS - 2
        // Use square-and-multiply (constant-time-ish)
        let mut result = Fq::one();
        let mut base = *self;
        let exponent = FIELD_MODULUS - 2;
        let mut e = exponent;
        while e > 0 {
            if e & 1 == 1 {
                result = result.mul(&base);
            }
            base = base.mul(&base);
            e >>= 1;
        }
        result
    }
}
