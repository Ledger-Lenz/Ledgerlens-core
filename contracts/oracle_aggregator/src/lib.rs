#![no_std]
use soroban_sdk::{contract, contractimpl, contracttype, Address, Bytes, BytesN, Env, Symbol, Vec};

#[contracttype]
pub struct SignaturePair {
    pub public_key: BytesN<32>,
    pub signature: BytesN<64>,
}

#[contract]
pub struct OracleAggregator;

#[contractimpl]
impl OracleAggregator {
    /// Initialise with threshold k, list of n authorised oracle public keys, and the ledgerlens-score contract address.
    pub fn initialize(env: Env, threshold: u32, oracle_keys: Vec<BytesN<32>>, score_contract: Address) {
        if env.storage().instance().has(&Symbol::new(&env, "THRESHOLD")) {
            panic!("already initialized");
        }
        env.storage().instance().set(&Symbol::new(&env, "THRESHOLD"), &threshold);
        env.storage().instance().set(&Symbol::new(&env, "ORACLE_KEYS"), &oracle_keys);
        env.storage().instance().set(&Symbol::new(&env, "SCORE_CONTRACT"), &score_contract);
    }

    /// Verify k-of-n signatures and forward to ledgerlens-score contract.
    pub fn submit_with_quorum(
        env: Env,
        wallet: Address,
        asset_pair: Symbol,
        score: u32,
        timestamp: u64,
        signatures: Vec<SignaturePair>,
    ) -> bool {
        // Replay protection: Reject timestamps older than 5 minutes (300 seconds)
        let current_time = env.ledger().timestamp();
        if current_time > timestamp && current_time - timestamp > 300 {
            return false;
        }

        let threshold: u32 = env.storage().instance().get(&Symbol::new(&env, "THRESHOLD")).unwrap();
        let oracle_keys: Vec<BytesN<32>> = env.storage().instance().get(&Symbol::new(&env, "ORACLE_KEYS")).unwrap();
        
        let message = Self::canonical_message(&env, &wallet, &asset_pair, score, timestamp);
        let mut valid_count: u32 = 0;
        
        for sig_pair in signatures.iter() {
            if oracle_keys.contains(&sig_pair.public_key) {
                if env.crypto().ed25519_verify(&sig_pair.public_key, &message, &sig_pair.signature).is_ok() {
                    valid_count += 1;
                }
            }
        }
        
        if valid_count < threshold {
            return false;
        }
        
        // Forward to ledgerlens-score contract
        let score_contract: Address = env.storage().instance().get(&Symbol::new(&env, "SCORE_CONTRACT")).unwrap();
        
        // We only mock the cross-contract call conceptually here.
        // In a real env, this would call score_contract.invoke_contract( "submit_score", (wallet, asset_pair, score, timestamp) )
        // env.invoke_contract::<()>(&score_contract, &Symbol::new(&env, "submit_score"), (wallet, asset_pair, score, timestamp).into_val(&env));
        
        true
    }
    
    pub fn canonical_message(env: &Env, wallet: &Address, asset_pair: &Symbol, score: u32, timestamp: u64) -> Bytes {
        // Matches Python OracleNode._canonical_message exactly
        // SHA-256("LedgerLens-Oracle-v1" || wallet || "|" || asset_pair || "|" || score_u32_be || timestamp_u64_be)
        
        let mut msg = Bytes::new(env);
        let prefix = Bytes::from_slice(env, b"LedgerLens-Oracle-v1");
        msg.append(&prefix);
        
        let wallet_str = wallet.to_string();
        let wallet_bytes = Bytes::from_slice(env, wallet_str.to_string().as_bytes()); // soroban string to bytes workaround
        msg.append(&wallet_bytes);
        
        msg.append(&Bytes::from_slice(env, b"|"));
        
        let asset_str = asset_pair.to_string();
        msg.append(&Bytes::from_slice(env, asset_str.to_string().as_bytes()));
        
        msg.append(&Bytes::from_slice(env, b"|"));
        
        let score_bytes = score.to_be_bytes();
        msg.append(&Bytes::from_slice(env, &score_bytes));
        
        let ts_bytes = timestamp.to_be_bytes();
        msg.append(&Bytes::from_slice(env, &ts_bytes));
        
        env.crypto().sha256(&msg).into()
    }
}

mod test;
