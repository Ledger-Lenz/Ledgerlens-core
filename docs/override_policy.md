# Manual Override & Suppression Policy (Issue #995)

Wallet allowlist/denylist overrides (`detection/wallet_override_store.py`,
`/admin/allowlist`, `/admin/denylist`) and alert suppressions
(`detection/suppressions.py`, `/admin/suppressions`) replace detection
results with a manual decision. One compromised or malicious admin account
must not be able to apply one on its own, so the following rules apply.

## Dual approval

- Every add or renew must name a requester (`added_by` / `requested_by` /
  `renewed_by`), a justification, and **at least two approvers**.
- Approvers must be distinct from each other (case-insensitive) and from the
  requester. A request with fewer than two approvers is rejected with HTTP 422
  and is never applied.
- Removing an override or deleting a suppression restores normal detection,
  so it needs no approvers. It is still audited.

## Expiry

- Every override and suppression expires. If `expires_at` is omitted it
  defaults to **90 days** from approval, and it can be set at most
  **180 days** out.
- Expired entries stop applying automatically. To keep one, renew it before
  expiry through `POST /admin/{allowlist,denylist}/{wallet}/renew` or
  `POST /admin/suppressions/{rule_id}/renew`. Renewal needs a fresh
  justification and the same dual approval as the original.
- Entries created before this policy had no expiry. On upgrade they get a
  90-day expiry, so each one must be re-reviewed and renewed.

## Audit trail

Every add, renew, and remove/delete writes a row to `override_audit_log`
with the requester, approvers, justification, and the before/after state of
the entry. Query it with:

- `GET /admin/overrides/audit?wallet=<wallet>`: wallet overrides
- `GET /admin/suppressions/audit?wallet=<wallet>`: suppressions

Approver identities are taken from the request body. Until per-user admin
authentication exists, the audit trail records who each request says
approved it; it does not prove identity.
