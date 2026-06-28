#![cfg(test)]

use super::*;
use soroban_sdk::{testutils::{Address as _, Ledger}, Address, BytesN, Env, Symbol, Vec};
use ed25519_dalek::{Keypair, Signer};
use rand::rngs::OsRng;

fn generate_keypair() -> Keypair {
    let mut csprng = OsRng{};
    Keypair::generate(&mut csprng)
}

#[test]
fn test_submit_with_quorum() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    let kp1 = generate_keypair();
    let kp2 = generate_keypair();
    let kp3 = generate_keypair();
    let kp4 = generate_keypair();
    let kp5 = generate_keypair();
    
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &kp1.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp2.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp3.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp4.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp5.public.to_bytes()));
    
    let score_contract = Address::generate(&env);
    client.initialize(&3, &oracle_keys, &score_contract);
    
    let wallet = Address::generate(&env);
    let asset_pair = Symbol::new(&env, "XLM-USDC");
    let score: u32 = 85;
    let timestamp: u64 = 1672531200;
    
    env.ledger().set_timestamp(timestamp + 100);
    
    let msg = client.canonical_message(&wallet, &asset_pair, &score, &timestamp);
    let msg_bytes = msg.to_alloc_vec();
    
    let sig1 = kp1.sign(&msg_bytes);
    let sig2 = kp2.sign(&msg_bytes);
    let sig3 = kp3.sign(&msg_bytes);
    
    let mut signatures = Vec::new(&env);
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp1.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig1.to_bytes()),
    });
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp2.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig2.to_bytes()),
    });
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp3.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig3.to_bytes()),
    });
    
    assert!(client.submit_with_quorum(&wallet, &asset_pair, &score, &timestamp, &signatures));
}

#[test]
fn test_rejects_n_minus_1_signatures() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    let kp1 = generate_keypair();
    let kp2 = generate_keypair();
    let kp3 = generate_keypair();
    
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &kp1.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp2.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp3.public.to_bytes()));
    
    let score_contract = Address::generate(&env);
    client.initialize(&3, &oracle_keys, &score_contract);
    
    let wallet = Address::generate(&env);
    let asset_pair = Symbol::new(&env, "XLM-USDC");
    let score: u32 = 85;
    let timestamp: u64 = 1672531200;
    
    env.ledger().set_timestamp(timestamp + 100);
    
    let msg = client.canonical_message(&wallet, &asset_pair, &score, &timestamp);
    let msg_bytes = msg.to_alloc_vec();
    
    let sig1 = kp1.sign(&msg_bytes);
    let sig2 = kp2.sign(&msg_bytes);
    
    let mut signatures = Vec::new(&env);
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp1.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig1.to_bytes()),
    });
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp2.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig2.to_bytes()),
    });
    
    assert!(!client.submit_with_quorum(&wallet, &asset_pair, &score, &timestamp, &signatures));
}

#[test]
fn test_rejects_forged_signature() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    let kp1 = generate_keypair();
    let kp2 = generate_keypair();
    let kp3 = generate_keypair();
    
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &kp1.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp2.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp3.public.to_bytes()));
    
    let score_contract = Address::generate(&env);
    client.initialize(&3, &oracle_keys, &score_contract);
    
    let wallet = Address::generate(&env);
    let asset_pair = Symbol::new(&env, "XLM-USDC");
    let score: u32 = 85;
    let timestamp: u64 = 1672531200;
    
    env.ledger().set_timestamp(timestamp + 100);
    
    let msg = client.canonical_message(&wallet, &asset_pair, &score, &timestamp);
    let msg_bytes = msg.to_alloc_vec();
    
    let sig1 = kp1.sign(&msg_bytes);
    let sig2 = kp2.sign(&msg_bytes);
    
    let mut bad_sig_bytes = [0u8; 64];
    bad_sig_bytes[0] = 42;
    
    let mut signatures = Vec::new(&env);
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp1.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig1.to_bytes()),
    });
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp2.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig2.to_bytes()),
    });
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp3.public.to_bytes()),
        signature: BytesN::from_array(&env, &bad_sig_bytes),
    });
    
    assert!(!client.submit_with_quorum(&wallet, &asset_pair, &score, &timestamp, &signatures));
}

#[test]
fn test_rejects_unknown_oracle_key() {
    let env = Env::default();
    let contract_id = env.register_contract(None, OracleAggregator);
    let client = OracleAggregatorClient::new(&env, &contract_id);
    
    let kp1 = generate_keypair();
    let kp2 = generate_keypair();
    let kp3 = generate_keypair(); // authorized
    let unknown_kp = generate_keypair(); // unauthorized
    
    let mut oracle_keys = Vec::new(&env);
    oracle_keys.push_back(BytesN::from_array(&env, &kp1.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp2.public.to_bytes()));
    oracle_keys.push_back(BytesN::from_array(&env, &kp3.public.to_bytes()));
    
    let score_contract = Address::generate(&env);
    client.initialize(&3, &oracle_keys, &score_contract);
    
    let wallet = Address::generate(&env);
    let asset_pair = Symbol::new(&env, "XLM-USDC");
    let score: u32 = 85;
    let timestamp: u64 = 1672531200;
    
    env.ledger().set_timestamp(timestamp + 100);
    
    let msg = client.canonical_message(&wallet, &asset_pair, &score, &timestamp);
    let msg_bytes = msg.to_alloc_vec();
    
    let sig1 = kp1.sign(&msg_bytes);
    let sig2 = kp2.sign(&msg_bytes);
    let unknown_sig = unknown_kp.sign(&msg_bytes);
    
    let mut signatures = Vec::new(&env);
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp1.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig1.to_bytes()),
    });
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &kp2.public.to_bytes()),
        signature: BytesN::from_array(&env, &sig2.to_bytes()),
    });
    signatures.push_back(SignaturePair {
        public_key: BytesN::from_array(&env, &unknown_kp.public.to_bytes()),
        signature: BytesN::from_array(&env, &unknown_sig.to_bytes()),
    });
    
    assert!(!client.submit_with_quorum(&wallet, &asset_pair, &score, &timestamp, &signatures));
}
