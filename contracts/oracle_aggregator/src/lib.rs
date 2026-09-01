#![no_std]

// #[contracttype], under the `testutils` feature (active for `cargo test` and
// for the fuzz sub-crate), additionally derives `arbitrary::Arbitrary`, whose
// generated code references `std` unconditionally even in a `#![no_std]`
// crate. Not activated by a plain build/release build. See the identical fix
// in contracts/zk_verifier/src/lib.rs.
#[cfg(any(test, feature = "testutils"))]
extern crate std;

use soroban_sdk::{
    auth::{ContractContext, InvokerContractAuthEntry, SubContractInvocation},
    contract, contractimpl, contracttype,
    xdr::ToXdr,
    Address, Bytes, BytesN, Env, IntoVal, String, Symbol, Val, Vec,
};

/// Domain separator. Must stay byte-identical to `detection/oracle_node.py`.
const DOMAIN_SEPARATOR: &[u8] = b"LedgerLens-Oracle-v2";

/// Maximum accepted timestamp age, in seconds, before a submission is treated
/// as a replay.
const MAX_TIMESTAMP_AGE_SECS: u64 = 300;

/// A Stellar strkey-encoded address is always 56 ASCII characters.
const ADDRESS_STRKEY_LEN: usize = 56;

#[contracttype]
#[derive(Clone)]
pub struct SignaturePair {
    pub public_key: BytesN<32>,
    pub signature: BytesN<64>,
}

#[contract]
pub struct OracleAggregator;

#[contractimpl]
impl OracleAggregator {
    /// Initialise with threshold k, list of n authorised oracle public keys, and the ledgerlens-score contract address.
    ///
    /// The `deployer` identity must authorise this call. Because (re)initialisation
    /// is permanently blocked after the first success, whoever presents a valid
    /// authorisation for `deployer` on the very first `initialize` call fixes the
    /// trusted oracle key set and quorum threshold for the lifetime of the
    /// contract. Front-running is mitigated by submitting deployment and this
    /// authorised call as tightly coupled as operationally possible (see
    /// `docs/oracle_quorum.md`).
    ///
    /// Panics if called twice, if `threshold` is zero or exceeds the number of
    /// authorised keys, or if `deployer` does not authorise the call — a quorum
    /// that cannot be met (or that any single caller can meet with no
    /// signatures) is always a configuration error.
    pub fn initialize(
        env: Env,
        deployer: Address,
        threshold: u32,
        oracle_keys: Vec<BytesN<32>>,
        score_contract: Address,
    ) {
        deployer.require_auth();

        if env.storage().instance().has(&Symbol::new(&env, "THRESHOLD")) {
            panic!("already initialized");
        }
        if threshold == 0 {
            panic!("threshold must be greater than zero");
        }
        if threshold > oracle_keys.len() {
            panic!("threshold exceeds number of oracle keys");
        }
        env.storage().instance().set(&Symbol::new(&env, "DEPLOYER"), &deployer);
        env.storage().instance().set(&Symbol::new(&env, "THRESHOLD"), &threshold);
        env.storage().instance().set(&Symbol::new(&env, "ORACLE_KEYS"), &oracle_keys);
        env.storage().instance().set(&Symbol::new(&env, "SCORE_CONTRACT"), &score_contract);
    }

    /// Return the `deployer` identity that authorised `initialize`.
    ///
    /// Lets operators verify, after deployment, which key controls the
    /// one-time configuration authority for this contract instance.
    pub fn get_deployer(env: Env) -> Address {
        env.storage()
            .instance()
            .get(&Symbol::new(&env, "DEPLOYER"))
            .unwrap_or_else(|| panic!("not initialized"))
    }

    /// Verify k-of-n signatures and forward to ledgerlens-score contract.
    ///
    /// Returns `false` (without trapping) when the submission is stale or when
    /// too few authorised oracles signed it.
    ///
    /// # Trapping behaviour
    ///
    /// `Env::crypto().ed25519_verify` returns `()` and **traps** on a bad
    /// signature — it cannot report failure as a value. This function therefore
    /// verifies only signatures whose public key is on the authorised list, and
    /// a malformed signature from an authorised key aborts the whole
    /// invocation rather than being skipped. Callers must not pad the
    /// `signatures` vector with junk from authorised keys. Signatures from
    /// unrecognised keys are ignored safely and never reach verification.
    ///
    /// # Quorum counting
    ///
    /// Each authorised public key contributes **at most one** vote. Repeating a
    /// key (or replaying one oracle's signature n times) cannot manufacture a
    /// quorum.
    ///
    /// # ledgerlens-score ABI and authorization
    ///
    /// The target ABI is:
    /// `submit_score(signers, wallet, asset_pair, score, benford_flag,
    /// ml_flag, timestamp, confidence, model_version, attestation_input)`.
    /// The aggregator derives `signers` as its own contract address and
    /// authorizes that exact sub-invocation with
    /// `authorize_as_current_contract`. Deployments must configure the
    /// aggregator address as the target contract's service address.
    ///
    /// `attestation_input` is deliberately `None`: the Ed25519 oracle quorum
    /// is the cryptographic attestation for this path. Consequently, the
    /// target's optional secp256k1 service-pubkey attestation must not be
    /// enabled for the aggregator service. If it is enabled, the target
    /// rejects the call and this invocation traps.
    ///
    /// Target contract errors and traps are deliberately propagated. Returning
    /// `false` after quorum would conflate "quorum rejected" with "approved but
    /// not persisted"; trapping rolls back the outer invocation atomically.
    #[allow(clippy::too_many_arguments)]
    pub fn submit_with_quorum(
        env: Env,
        wallet: Address,
        asset_pair: Symbol,
        score: u32,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: u64,
        confidence: u32,
        model_version: u32,
        signatures: Vec<SignaturePair>,
    ) -> bool {
        // Replay protection: reject timestamps older than MAX_TIMESTAMP_AGE_SECS.
        let current_time = env.ledger().timestamp();
        if current_time > timestamp && current_time - timestamp > MAX_TIMESTAMP_AGE_SECS {
            return false;
        }

        let threshold: u32 = env
            .storage()
            .instance()
            .get(&Symbol::new(&env, "THRESHOLD"))
            .unwrap();
        let oracle_keys: Vec<BytesN<32>> = env
            .storage()
            .instance()
            .get(&Symbol::new(&env, "ORACLE_KEYS"))
            .unwrap();

        let message = Self::build_canonical_message(
            &env,
            &wallet,
            &asset_pair,
            score,
            benford_flag,
            ml_flag,
            timestamp,
            confidence,
            model_version,
        );

        // Track which authorised keys have already been counted so a repeated
        // key cannot contribute more than one vote toward the quorum.
        let mut counted: Vec<BytesN<32>> = Vec::new(&env);

        for sig_pair in signatures.iter() {
            if !oracle_keys.contains(&sig_pair.public_key) {
                continue;
            }
            if counted.contains(&sig_pair.public_key) {
                continue;
            }
            // Traps if the signature is invalid; see the note above.
            env.crypto()
                .ed25519_verify(&sig_pair.public_key, &message, &sig_pair.signature);
            counted.push_back(sig_pair.public_key.clone());
        }

        if counted.len() < threshold {
            return false;
        }

        let score_contract: Address = env
            .storage()
            .instance()
            .get(&Symbol::new(&env, "SCORE_CONTRACT"))
            .unwrap();
        let function_name = Symbol::new(&env, "submit_score");
        let service = env.current_contract_address();
        let mut signers = Vec::new(&env);
        signers.push_back(service);

        // `Option<T>::None` always encodes as SCV_VOID. `Bytes` supplies the
        // otherwise-unobservable inner type without duplicating the target
        // repository's evolving ScoreAttestationInput definitions here.
        let no_attestation: Option<Bytes> = None;
        let args: Vec<Val> = soroban_sdk::vec![
            &env,
            signers.into_val(&env),
            wallet.into_val(&env),
            asset_pair.into_val(&env),
            score.into_val(&env),
            benford_flag.into_val(&env),
            ml_flag.into_val(&env),
            timestamp.into_val(&env),
            confidence.into_val(&env),
            model_version.into_val(&env),
            no_attestation.into_val(&env),
        ];

        env.authorize_as_current_contract(soroban_sdk::vec![
            &env,
            InvokerContractAuthEntry::Contract(SubContractInvocation {
                context: ContractContext {
                    contract: score_contract.clone(),
                    fn_name: function_name.clone(),
                    args: args.clone(),
                },
                sub_invocations: Vec::new(&env),
            }),
        ]);
        env.invoke_contract::<()>(&score_contract, &function_name, args);

        true
    }

    /// Exported view of the canonical signing preimage digest.
    ///
    /// Off-chain signers (see `detection/oracle_node.py`) must sign exactly the
    /// 32 bytes this returns. Exposing it on the contract client lets the
    /// Python and Rust encodings be diffed directly instead of only surfacing a
    /// mismatch later as an opaque signature-verification trap.
    ///
    /// Takes owned arguments: `#[contractimpl]` cannot export a function whose
    /// parameters are references (`rustc` rejects it with "unsupported type"),
    /// which is why this was previously absent from the generated client.
    #[allow(clippy::too_many_arguments)]
    pub fn canonical_message(
        env: Env,
        wallet: Address,
        asset_pair: Symbol,
        score: u32,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: u64,
        confidence: u32,
        model_version: u32,
    ) -> Bytes {
        Self::build_canonical_message(
            &env,
            &wallet,
            &asset_pair,
            score,
            benford_flag,
            ml_flag,
            timestamp,
            confidence,
            model_version,
        )
    }
}

impl OracleAggregator {
    /// Internal by-reference implementation shared by `submit_with_quorum` and
    /// the exported `canonical_message`. Lives outside `#[contractimpl]` so it
    /// can take references without tripping the export type check.
    ///
    /// Byte layout, which must stay identical to the Python implementation:
    ///
    /// ```text
    /// SHA-256(
    ///     "LedgerLens-Oracle-v2"
    ///     || wallet_strkey_utf8
    ///     || "|"
    ///     || asset_pair_scval_xdr
    ///     || "|"
    ///     || score  as u32 big-endian
    ///     || benford_flag as u8
    ///     || ml_flag as u8
    ///     || timestamp as u64 big-endian
    ///     || confidence as u32 big-endian
    ///     || model_version as u32 big-endian
    /// )
    /// ```
    #[allow(clippy::too_many_arguments)]
    fn build_canonical_message(
        env: &Env,
        wallet: &Address,
        asset_pair: &Symbol,
        score: u32,
        benford_flag: bool,
        ml_flag: bool,
        timestamp: u64,
        confidence: u32,
        model_version: u32,
    ) -> Bytes {
        let mut msg = Bytes::new(env);
        msg.append(&Bytes::from_slice(env, DOMAIN_SEPARATOR));

        let mut wallet_buf = [0u8; ADDRESS_STRKEY_LEN];
        Self::append_string(env, &mut msg, &wallet.to_string(), &mut wallet_buf);

        msg.append(&Bytes::from_slice(env, b"|"));

        msg.append(&asset_pair.clone().to_xdr(env));

        msg.append(&Bytes::from_slice(env, b"|"));
        msg.append(&Bytes::from_slice(env, &score.to_be_bytes()));
        msg.append(&Bytes::from_slice(env, &[benford_flag as u8]));
        msg.append(&Bytes::from_slice(env, &[ml_flag as u8]));
        msg.append(&Bytes::from_slice(env, &timestamp.to_be_bytes()));
        msg.append(&Bytes::from_slice(env, &confidence.to_be_bytes()));
        msg.append(&Bytes::from_slice(env, &model_version.to_be_bytes()));

        env.crypto().sha256(&msg).into()
    }

    /// Append a Soroban `String`'s UTF-8 bytes to `msg`.
    ///
    /// `String::copy_into_slice` requires an exactly-sized destination, and this
    /// crate is `no_std` without an allocator, so the caller supplies a
    /// stack buffer that bounds the accepted length.
    fn append_string(env: &Env, msg: &mut Bytes, value: &String, buf: &mut [u8]) {
        let len = value.len() as usize;
        if len > buf.len() {
            panic!("string exceeds canonical message buffer");
        }
        let slice = &mut buf[..len];
        value.copy_into_slice(slice);
        msg.append(&Bytes::from_slice(env, slice));
    }
}

#[cfg(test)]
mod test;
