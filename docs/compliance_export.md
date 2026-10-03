# Compliance Export Pipeline

LedgerLens can package its detection evidence into the formats exchanges and
custodians need to meet two distinct regulatory obligations:

- **FATF Travel Rule** (Recommendation 16) — sharing originator/beneficiary
  risk information for qualifying transfers.
- **FinCEN Suspicious Activity Reports (SAR, Form 111)** — a documented
  narrative and evidence package supporting a filing.

All three endpoints live in `detection/compliance_exporter.py` and are
mounted under `/compliance/*` in `api/main.py`, gated by the
`X-LedgerLens-Compliance-Key` header (see [api_reference.md](api_reference.md)).
They are excluded from the OpenAPI schema (`include_in_schema=False`) so they
never surface on the unauthenticated `/docs` page.

## Legal disclaimer

**LedgerLens is a detection and evidence-packaging tool, not a compliance
advisor.** It does not file SARs or Travel Rule reports — that is the
responsibility of the regulated institution. Generated narratives and
records must be independently reviewed and verified by qualified compliance
personnel before submission to a regulator. Nothing in this pipeline
constitutes legal advice.

## Endpoints

### `GET /compliance/ivms/{wallet}`

Returns an `IVMSRiskField` (`ledgerlens_score`, `risk_level`, `alert_types`,
`score_timestamp`, `evidence_hash`) suitable for injecting into an existing
IVMS 101 Travel Rule payload via `augment_ivms_payload`. `evidence_hash` is a
SHA-256 commitment over the scored fact, mirroring the on-chain ZK proof
commitment so a Travel Rule recipient can cross-reference the two without
either party revealing the underlying score history.

### `POST /compliance/sar-narrative/draft` (v1)

Body: `{"wallet": str, "start_date": ISO8601, "end_date": ISO8601}`.

Returns `{"draft": str, "draft_sha256": str}`: the auto-generated narrative
for analyst review. A draft is **never exportable** on its own; see
[SAR narrative review workflow](#sar-narrative-review-workflow).

### `POST /compliance/sar-package`

Body: `{"wallet": str, "start_date": ISO8601, "end_date": ISO8601,
"reviewer": str, "draft_sha256": str, "approved_narrative": str | null,
"review_notes": str}`.

Returns `403` if `reviewer` or `draft_sha256` is missing, and `409` if the
draft has changed since it was reviewed (for example, new alerts arrived).

Generates a ZIP archive (`sar_narrative.txt`, `sar_review.json`, `evidence/alerts.json`,
`evidence/score_history.csv`, `evidence/graph_export.gexf`,
`evidence/shap_explanations.json`, `manifest.json` with a SHA-256 of every
included file) and returns it as a download.

Guardrails, since SAR generation triggers SHAP recomputation and graph
construction and is not cheap to run on demand:

- **Minimum risk score** — the wallet's current peak risk score must be at
  least `COMPLIANCE_SAR_MIN_SCORE` (default 70), or the request fails with
  `400`. A SAR shouldn't be trivial to generate for a wallet LedgerLens
  hasn't actually flagged as high-risk.
- **Rate limit** — `COMPLIANCE_EXPORT_RATE_LIMIT_PER_HOUR` exports/hour
  (default 100) across all SAR + Travel Rule exports combined, counted from
  the `compliance_exports` audit table (not in-memory, so it holds across
  restarts and multiple API instances). Exceeding it returns `429`.

### `GET /compliance/audit-trail/{wallet}`

Returns the full chronological event log for a wallet (risk scores, alerts,
on-chain submissions, disputes, score overrides) -- for a legal hold, not a
record of exports themselves (see below).

## SAR narrative review workflow

SAR narratives are legally significant, so an auto-generated narrative can
never be exported without a recorded human review. Every export path
(`generate_sar_package`, `export_sar_package` and both `sar-package` endpoints)
obtains the narrative through `detection.sar_narrative.require_approved_narrative`,
which raises `SARNarrativeNotReviewed` unless it gets a `SARNarrativeReview`
bound, by SHA-256, to the current draft.

For compliance analysts:

1. **Fetch the draft**: `POST /v1/compliance/sar-narrative/draft` (or
   `draft_sar_narrative()` in Python). Keep the returned `draft_sha256`.
2. **Review it** against the evidence (alerts, score history, graph, SHAP).
   Correct anything the model got wrong.
3. **Approve it**: `POST /compliance/sar-package` with your identity in
   `reviewer`, the `draft_sha256` you reviewed, and your corrected text in
   `approved_narrative` (omit it to approve the draft unchanged). In Python,
   call `review_sar_narrative(draft, reviewer, edited_text=...)` and pass the
   result as `review=` to `export_sar_package`.
4. If the export returns `409`, the underlying data changed after your
   review: fetch and review the new draft.

The package's `sar_review.json` records the reviewer, review timestamp,
draft and final SHA-256, whether it was edited, a unified diff of the
analyst's edits, and any notes. The file is covered by the integrity
`manifest.json`, so the review record is tamper-evident alongside the
narrative it approves.

## Dry-run mode

Both export endpoints (`/compliance/ivms/{wallet}`,
`/compliance/sar-package`) accept `?dry_run=true`. The response is identical
to a normal call; the only difference is that the export is **not** recorded
to the `compliance_exports` audit table.

**Limitation:** because dry-run exports leave no audit trail, they must not
be used to produce the copy of a document that actually gets submitted to a
regulator. Use dry-run only to preview output during integration testing.

## Export audit log

Every non-dry-run export (SAR or Travel Rule) is recorded to the
`compliance_exports` SQLite table:

| Column | Notes |
|---|---|
| `export_type` | `"sar"` or `"travel_rule"` |
| `wallet_hash` | SHA-256 of the wallet address -- **never the plaintext address** |
| `asset_pairs` | JSON array, if applicable |
| `risk_score` | the wallet's score at export time |
| `exported_at` | ISO 8601 UTC timestamp |
| `dry_run` | always `0` for logged rows (dry-run exports are never logged at all) |

See [database_schema.md](database_schema.md) for the full table definition.

## Configuration

```bash
COMPLIANCE_SAR_MIN_SCORE=70                  # minimum risk score required to generate a SAR
COMPLIANCE_EXPORT_RATE_LIMIT_PER_HOUR=100    # combined SAR + Travel Rule exports/hour
LEDGERLENS_COMPLIANCE_API_KEY=your-compliance-key
```

## PII handling

- Originator/beneficiary names are not handled by these endpoints today --
  `build_ivms_risk_field`/`augment_ivms_payload` only attach LedgerLens's own
  risk evidence to an IVMS payload the caller supplies; LedgerLens never
  generates the personal-data fields itself.
- Stellar public keys are pseudonymous, but the audit log still stores only
  a SHA-256 hash, never the plaintext address, so the audit trail can't
  itself become a source of wallet-to-export linkage.

## Pluggable export formats

`detection/compliance_formats.py` exposes a format-plugin registry. Select a
format at export time with
`export_risk_assessment(wallet, fmt="ivms101" | "goaml", **options)`; every
payload is validated against the plugin's JSON Schema before it is returned
(`ExportValidationError` on failure).

| Format    | Standard                                                        |
|-----------|-----------------------------------------------------------------|
| `ivms101` | FATF Travel Rule IVMS 101 LedgerLens risk block                 |
| `goaml`   | UNODC goAML 4.x `report` (JSON projection), used by 60+ FIUs     |

`goaml` options: `rentity_id` (FIU-assigned reporting entity id),
`currency_code_local` (ISO 4217, default `USD`), `report_code` (default `STR`).

To add a format, subclass `ExportFormat`, set `name` and `schema`, implement
`build()`, and decorate the class with `@register_format` — no changes to the
core exporter are required.
