# Break-Glass Admin Access

Sensitive admin actions carry no standing privilege. They require a recorded
justification and a short-lived elevation token, and every use is written to
the HMAC-chained audit log (`storage/audit_log.py`), separate from routine
request logging.

## Sensitive actions

| Endpoint | Action |
|---|---|
| `POST /admin/models/{version}/promote` | Promote a model version |
| `PATCH /admin/config` | Change runtime config |
| `POST /admin/retrain` | Trigger retraining |
| `POST /admin/suppressions` | Add an alert suppression rule |
| `DELETE /admin/suppressions/{rule_id}` | Remove an alert suppression rule |

## Flow

1. Request elevation (admin key required):

   ```http
   POST /admin/elevate
   X-LedgerLens-Admin-Key: <key>

   {"justification": "INC-42: roll back model 2.3.1 after drift alert"}
   ```

   Response: `{"elevation_token": "...", "expires_at": "<ISO-8601 UTC>"}`

2. Call the sensitive endpoint with both headers:

   ```http
   X-LedgerLens-Elevation-Token: <token>
   X-LedgerLens-Justification: INC-42: roll back model 2.3.1
   ```

## Policy

- Justifications must be at least 10 characters. A missing or short
  justification returns **400**.
- A missing, unknown, or expired elevation token returns **403**.
- Tokens expire after `ADMIN_ELEVATION_TTL_SECONDS` (default **900**, i.e.
  15 minutes). Expired tokens are purged on the next check. Tokens live in
  process memory, so a restart revokes all of them.
- Each elevation and each sensitive action appends a
  `break_glass_admin_action` entry to the audit log with the actor (a
  non-secret fingerprint of the admin key), the timestamp, and the
  justification (prefixed with the HTTP method and path). The justification
  is covered by the entry HMAC, so changing it breaks `verify_chain`.
