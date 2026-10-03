# STRIDE Threat → Test Traceability Matrix

Maps every STRIDE threat in [threat_model.md](threat_model.md) to the automated
regression test(s) that would catch it regressing, or to an explicit, tracked
gap.

**Threat IDs** are `B<boundary>-<letter>`: the boundary number from
`### Boundary N` in the threat model, plus the STRIDE letter of the row
(`S`poofing, `T`ampering, `R`epudiation, `I`nformation disclosure,
`D`enial of service, `E`levation of privilege).

**Status** is `covered` (at least one test listed) or `gap` (no regression test
yet; the Follow-up column must name the tracking entry in
[`TODO.md`](../TODO.md)).

## Enforcement

`scripts/check_threat_matrix.py` runs in CI (`ci.yml` → *Threat matrix*) and fails
the build when:

- a STRIDE row in `threat_model.md` has no row here (so **adding a threat
  requires adding its test link, or a tracked gap, in the same PR**);
- a row here references a test file or test function that does not exist;
- a `gap` row has no follow-up, or its follow-up ID is not present in `TODO.md`.

Test references use `path::test_name` (Python `def`/Rust `fn`) or a bare path
for a whole file or CI workflow.

## Matrix

| ID | Threat | Test(s) | Status | Follow-up |
|---|---|---|---|---|
| B1-S | RPC endpoint spoofing (falsified history / lag) | `tests/test_evm_provider_pool.py::test_lagging_provider_penalised`, `tests/test_evm_provider_pool.py::test_healthy_provider_full_score` | covered | |
| B1-T | Tampered Horizon / Solana VAA payloads | `tests/test_solana_adapter.py::test_crc16_xmodem_known_vector`, `tests/test_fuzz_regression.py`, `.github/workflows/nightly_fuzz.yml` | covered | |
| B1-R | Untraceable provider for ingested data | `tests/test_evm_provider_pool.py::test_all_providers_fail_raises_exhausted` | covered | |
| B1-I | RPC URL / API key disclosure | `tests/test_evm_provider_pool.py::test_masks_api_key_in_path`, `tests/test_evm_provider_pool.py::test_exhausted_error_does_not_contain_rpc_url` | covered | |
| B1-D | Failing RPC node stalls ingestion | `tests/test_evm_loader.py::test_circuit_breakers_are_independent`, `tests/test_evm_loader.py::test_token_bucket_sleeps_when_empty` | covered | |
| B1-E | Malformed JSON-RPC params injection | `tests/test_evm_provider_pool.py::test_invalid_params_rejected_before_rpc` | covered | |
| B2-S | Webhook replay | `tests/test_webhook_security.py::test_timestamp_window_past`, `tests/test_webhook_security.py::test_missing_timestamp_equivalent_to_zero_rejected` | covered | |
| B2-T | Webhook payload tampering | `tests/test_webhook_security.py::test_tampered_body_fails` | covered | |
| B2-R | Subscriber denies receiving alert | `tests/test_webhook_worker.py::test_deliver_moves_to_dead_after_8_attempts`, `tests/test_webhook_queue.py::test_get_dead_letters_returns_only_dead` | covered | |
| B2-I | Sensitive metadata in worker logs | `tests/test_log_no_wallet_addresses.py` | covered | |
| B2-D | Slow endpoint exhausts dispatch | `tests/test_webhook_worker.py::test_deliver_http_500_triggers_retry`, `tests/test_webhook_queue.py::test_mark_failed_exponential_backoff_caps_at_one_hour` | covered | |
| B2-E | SSRF via subscriber URL | `tests/test_webhook_registry.py::test_ssrf_rejects_localhost`, `tests/test_webhook_registry.py::test_ssrf_rejects_private_ip_10` | covered | |
| B3-S | Single compromised oracle node | `contracts/oracle_aggregator/src/test.rs::unauthorised_keys_do_not_count_toward_quorum`, `contracts/oracle_aggregator/src/test.rs::repeated_key_cannot_forge_quorum` | covered | |
| B3-T | Tampered oracle submission | `contracts/oracle_aggregator/src/test.rs::signature_over_different_payload_is_not_accepted`, `contracts/oracle_aggregator/src/test.rs::canonical_message_is_sensitive_to_every_field` | covered | |
| B3-R | Oracle node denies publishing | `contracts/oracle_aggregator/src/test.rs::malformed_signature_from_authorised_key_traps` | covered | |
| B3-I | Service secret key leak | `tests/test_soroban_publisher.py::test_secret_key_not_in_logs` | covered | |
| B3-D | Soroban RPC failures freeze pipeline | `tests/test_soroban_circuit_breaker_dlq.py::test_circuit_opens_after_threshold_failures`, `tests/test_soroban_circuit_breaker_dlq.py::test_dlq_written_when_circuit_open` | covered | |
| B3-E | Unprivileged direct score submission | `contracts/oracle_aggregator/src/test.rs::failed_quorum_does_not_invoke_score_contract`, `contracts/oracle_aggregator/src/test.rs::unauthorized_caller_cannot_initialize` | covered | |
| B4-S | Admin key guessed / stolen | `tests/test_api_gateway.py::test_gateway_rejects_invalid_key`, `tests/test_api_gateway.py::test_gateway_admin_key_access` | covered | |
| B4-T | Governance disables key checks | `tests/test_governance.py::test_secret_key_rejected`, `tests/test_governance.py::test_admin_key_rejected` | covered | |
| B4-R | Admin denies executing action | `tests/test_audit_log.py::test_log_admin_config_changed`, `tests/test_audit_log.py::test_verify_chain_detects_tampered_entry_hash` | covered | |
| B4-I | Unauthenticated metrics scraping | | gap | B4-I |
| B4-D | Request flood on admin/explain endpoints | `tests/test_api_gateway.py::test_gateway_per_minute_rate_limit`, `tests/test_rate_limiter.py::test_two_replica_processes_share_one_effective_quota` | covered | |
| B4-E | Consumer calls admin endpoints | `tests/test_api_gateway.py::test_gateway_rejects_wrong_scope`, `tests/test_api_gateway.py::test_gateway_compliance_key_access` | covered | |
| B5-S | Unauthorised FL participant | `tests/test_federated_admission.py::test_is_admitted_false_for_unknown_participant` | covered | |
| B5-T | Poisoned FL updates | `tests/test_federated_server.py::test_norm_clipping_rejects_large_gradient`, `tests/test_federated_server.py::test_cosine_outlier_excludes_adversarial`, `tests/test_krum_aggregation.py` | covered | |
| B5-R | Participant denies poisoned submission | `tests/test_federated_server.py::test_audit_record_created_and_signed`, `tests/test_federated_audit.py::test_audit_records_verify_with_server_public_key` | covered | |
| B5-I | Server reconstructs client data | `tests/test_federated_dp.py::test_client_noise_variance_matches_nm` | covered | |
| B5-D | Participant dropout stalls round | `tests/test_krum_aggregation.py::test_m_exceeds_n_minus_f_raises` | covered | |
| B5-E | Participant reads others' gradients | | gap | B5-E |
| B6-S | Malicious model file placed | `tests/test_ed25519_model_signing.py::test_missing_sig_raises`, `tests/test_model_signing.py::test_missing_sig_file_raises` | covered | |
| B6-T | `.joblib` overwritten with RCE payload | `tests/test_ed25519_model_signing.py::test_tampered_model_raises`, `tests/test_model_signing.py::test_tampered_file_safe_load_raises` | covered | |
| B6-R | Untraceable model artifact | `tests/test_ed25519_model_signing.py::test_verify_models_tampered_exits_nonzero` | covered | |
| B6-I | Signing key exposed in logs / disk | | gap | B6-I |
| B6-D | Bad signatures freeze reload | `tests/test_ed25519_model_signing.py::test_verify_models_all_valid`, `tests/test_ed25519_model_signing.py::test_verify_models_tampered_exits_nonzero` | covered | |
| B6-E | Compromised third-party dependency | `.github/workflows/ci.yml`, `.github/workflows/license-vuln-scan.yml` | covered | |
