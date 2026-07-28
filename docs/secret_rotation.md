# Secret Rotation Runbook

This runbook outlines the lifecycle, configuration, and step-by-step procedures for rotating the four secret types managed in LedgerLens:
1. **Scoped API Keys** (`detection/api_key_store.py`)
2. **Namespace API Keys** (`api/namespace.py`)
3. **Webhook HMAC Encryption Key** (`LEDGERLENS_WEBHOOK_ENCRYPTION_KEY`)
4. **Soroban Service Account Key** (`LEDGERLENS_SERVICE_SECRET_KEY`)

---

## 1. Scoped API Keys
Scoped API keys use an overlapping validity grace window during rotation.

### Lifecycle
```
[Active Key (Key A)] --(POST /admin/api-keys/{id}/rotate)--> [Key A (Rotating)] + [New Key B (Active)]
                                                                   |
                                                         (grace_period elapsed)
                                                                   v
                                                            [Key A (Revoked)]
```

### Rotation Procedure
Execute the rotation endpoint with the target key's ID:
```bash
curl -X POST "https://<ledgerlens-host>/admin/api-keys/<key_id>/rotate?grace_period_seconds=604800" \
     -H "X-LedgerLens-Admin-Key: <admin_key>"
```
The response will return the new plaintext API key exactly once. Both keys remain valid until the grace period passes.

### Sweep Task
To clean up expired rotating keys, run the sweep command (or schedule it as a cron task):
```bash
python cli.py rotate-sweep
```

---

## 2. Namespace API Keys
Namespace API keys isolate tenant metrics. They follow a similar grace-window pattern.

### Rotation Procedure
Rotate the active key for a given namespace:
```bash
curl -X POST "https://<ledgerlens-host>/admin/namespaces/<namespace_id>/rotate-key?grace_period_seconds=604800" \
     -H "X-LedgerLens-Admin-Key: <admin_key>"
```

---

## 3. Webhook HMAC Encryption Key
The webhook subscriber secrets are encrypted using AES-256-GCM. Rotating this key requires dual key support to avoid decryption failures during cutover.

### Rotation Procedure
1. Set the current key as the previous key and generate a new key as current:
   - Current Key: `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY`
   - Previous Key: `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY_PREVIOUS`
2. Deploy the app with both environment variables populated. Decryptions will fall back to the previous key if current fails.
3. Run the re-encryption command:
   ```bash
   python cli.py re-encrypt-webhook-secrets
   ```
4. Verify that all rows are re-encrypted.
5. Remove `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY_PREVIOUS` from the environment and redeploy.

---

## 4. Soroban Service Account Key
The Soroban service account key is an on-chain authorized Stellar account key. Full automation is not possible due to contract-level authorization ownership residing in `ledgerlens-contracts`.

### Lifecycle Diagram
```
[Old Key (Authorized)] 
     |
     +--> Generate New Keypair
     +--> Submit set_options / contract authorization change on-chain
     |
[Dual-Signature Window: Both Old and New Keys Authorized]
     |
     +--> Deploy New Key to LedgerLens
     +--> Submit test submit_score() under New Key
     |
[Retire Old Key: Remove On-Chain Authorization]
```

### Step-by-Step Operator Runbook
1. **Generate Keypair**: Generate a new Stellar keypair.
2. **On-chain Authorization**: Submit a transaction to authorize the new public key to call `submit_score()` on-chain via the appropriate `ledgerlens-contracts` script.
3. **Dual-Sign Window**: Ensure both the old and new keys are authorized simultaneously.
4. **Deploy & Test**: Set the new secret key as `LEDGERLENS_SERVICE_SECRET_KEY` in the environment, restart the process, and submit a test score transaction on Testnet.
5. **Retire Old Key**: Once verified, submit an on-chain transaction to revoke the old key's authorization.

---

## Operator Checklist
- [ ] Monitor `ledgerlens_secret_rotation_total` for success and failure counts.
- [ ] Respond immediately to `SecretRotationOverdue` alert.
- [ ] Limit the period when both `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY` and `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY_PREVIOUS` are loaded.
