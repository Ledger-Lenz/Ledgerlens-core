---
title: "Build FATF Travel Rule and FinCEN SAR Compliance Export Pipeline"
labels: ["difficulty: advanced", "area: compliance", "type: feature"]
assignees: []
---

## Summary

Extend `detection/compliance_exporter.py` to generate structured FinCEN SAR narratives and FATF Travel Rule records from high-risk-score events. SAR narrative templates should include wallet IDs, asset pairs, detected ring structure, Benford metrics, and SHAP top-3 features. This positions LedgerLens as a compliance infrastructure layer usable by Stellar-based exchanges and custodians subject to AML/CFT obligations.

## Background & Context

Exchanges and custodians operating on the Stellar network may be obligated under the Financial Action Task Force (FATF) Travel Rule (Recommendation 16) and FinCEN SAR filing requirements. The Travel Rule requires sharing originator/beneficiary information for transfers above threshold amounts. SARs require a detailed narrative of suspicious activity.

LedgerLens is uniquely positioned to feed both requirements: it has the detection evidence (Benford metrics, ring structure, SHAP explanations) and the wallet identifiers. By automating the export pipeline, compliance officers at exchanges can reduce SAR preparation time from hours to minutes.

`detection/compliance_exporter.py` exists as a stub. This issue builds:
1. **FinCEN SAR narrative generator**: structured narrative text from a `RiskScore` + supporting evidence
2. **FATF Travel Rule record**: ISO 20022-inspired structured data for originator/beneficiary transfers
3. **Export API**: `POST /compliance/export/sar` and `POST /compliance/export/travel-rule`
4. **Audit log**: every export is timestamped and logged to SQLite `compliance_exports` table

Note: this is a data structuring and export tool. LedgerLens does not file SARs — that is done by the regulated entity. All PII fields are caller-supplied; LedgerLens supplies the detection evidence.

## Objectives

- [ ] Implement `SARNarrativeBuilder` that produces a FinCEN SAR-format narrative from a `RiskScore` and supporting evidence
- [ ] Implement `TravelRuleRecordBuilder` that produces a FATF-compliant JSON record from transfer metadata
- [ ] Implement `ComplianceExporter.export_sar(wallet, evidence)` returning a structured SAR document
- [ ] Implement `ComplianceExporter.export_travel_rule(transfer)` returning a structured Travel Rule record
- [ ] Add `POST /compliance/export/sar` and `POST /compliance/export/travel-rule` endpoints
- [ ] Log all exports to `compliance_exports` SQLite table with redacted wallet identifiers
- [ ] Write tests validating SAR narrative completeness, Travel Rule field coverage, and audit logging
- [ ] Implement a dry-run mode (`?dry_run=true`) that returns the export without writing to the audit log

## Technical Requirements

### SAR narrative structure

```python
# detection/compliance_exporter.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class SAREvidence:
    risk_score: int
    benford_flags: dict[str, float]    # {window: chi_sq or mad value}
    ring_structure: Optional[dict]     # from graph_engine SCC output
    shap_top3: list[tuple[str, float]] # [(feature_name, shap_value), ...]
    ml_flag: bool
    benford_flag: bool
    confidence: int
    score_lower: Optional[float]
    score_upper: Optional[float]
    first_flagged_at: datetime
    total_trades_analyzed: int

@dataclass
class SARNarrative:
    filing_institution_name: str        # caller-supplied
    subject_wallet: str                 # Stellar public key
    subject_role: str                   # "originator" | "both" | "cluster_member"
    asset_pairs_involved: list[str]
    activity_period_start: datetime
    activity_period_end: datetime
    narrative_text: str                 # generated paragraph
    evidence_summary: dict              # machine-readable evidence
    generated_at: datetime = field(default_factory=datetime.utcnow)
    sar_form_version: str = "FinCEN SAR 2022"
```

### SAR narrative template

```python
SAR_NARRATIVE_TEMPLATE = """
Subject {subject_role} {subject_wallet_short} engaged in suspicious trading activity on the
Stellar Decentralized Exchange (SDEX) involving asset pair(s) {asset_pairs} during the period
{start_date} to {end_date}.

Detection summary: LedgerLens risk score {score}/100 (90% confidence interval: {lower}–{upper}).
{benford_summary}
{ring_summary}
{ml_summary}

Top contributing factors (SHAP analysis):
{shap_bullets}

This report was generated automatically by LedgerLens v{version}. The filing institution
is responsible for independent review and verification before SAR submission.
"""

class SARNarrativeBuilder:
    def build(
        self,
        wallet: str,
        evidence: SAREvidence,
        institution_name: str,
        asset_pairs: list[str],
        activity_start: datetime,
        activity_end: datetime,
    ) -> SARNarrative:
        ...

    def _benford_summary(self, flags: dict[str, float]) -> str:
        """
        E.g.: "Benford's Law analysis detected significant digit-distribution anomalies:
        chi-square 24h = 87.3 (expected < 15.5 for genuine trading at p=0.05)."
        """
        ...

    def _ring_summary(self, ring: Optional[dict]) -> str:
        """
        E.g.: "Graph analysis identified membership in a wash ring of 7 accounts with
        cycle volume of 45,000 XLM (87% of total outbound volume)."
        Returns empty string if no ring detected.
        """
        ...

    def _shap_bullets(self, shap_top3: list[tuple[str, float]]) -> str:
        """
        Returns bullet-point string. Feature names are mapped to plain-language descriptions.
        E.g.: "• Round-trip trade frequency: 85% (contribution +23 score points)"
        """
        ...
```

### FATF Travel Rule record

```python
@dataclass
class TravelRuleTransfer:
    transfer_id: str                  # caller-supplied (e.g., Horizon operation ID)
    originator_wallet: str
    originator_name: Optional[str]    # caller-supplied; may be None if pseudonymous
    originator_vasp_name: str         # originating VASP name
    originator_vasp_lei: Optional[str]
    beneficiary_wallet: str
    beneficiary_name: Optional[str]
    beneficiary_vasp_name: str
    asset: str                        # e.g., "XLM", "USDC"
    amount: float
    amount_usd_equiv: Optional[float]
    transfer_timestamp: datetime
    ledger_sequence: int
    ledgerlens_risk_score: Optional[int]
    ledgerlens_flagged: bool

@dataclass
class TravelRuleRecord:
    transfer: TravelRuleTransfer
    record_format: str = "IVMS101"    # InterVASP Messaging Standard
    schema_version: str = "1.0"
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_json(self) -> dict:
        """Serialise to IVMS101-compatible JSON structure."""
        ...
```

### API endpoints

```python
@router.post("/compliance/export/sar")
async def export_sar(
    body: SARExportRequest,
    dry_run: bool = Query(False),
    x_admin_key: str = Header(..., alias="X-LedgerLens-Admin-Key"),
) -> SARExportResponse:
    """
    Generate SAR narrative for a wallet.
    Requires wallet to have a current RiskScore with score >= SAR_MIN_SCORE (default 70).
    dry_run=true returns the narrative without audit logging.
    """
    ...

@router.post("/compliance/export/travel-rule")
async def export_travel_rule(
    body: TravelRuleExportRequest,
    dry_run: bool = Query(False),
    x_admin_key: str = Header(..., alias="X-LedgerLens-Admin-Key"),
) -> TravelRuleExportResponse:
    ...
```

### Audit log schema

```sql
CREATE TABLE IF NOT EXISTS compliance_exports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    export_type   TEXT NOT NULL,        -- "sar" | "travel_rule"
    wallet_hash   TEXT NOT NULL,        -- SHA-256 of wallet address (not plaintext)
    asset_pairs   TEXT,
    risk_score    INTEGER,
    exported_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dry_run       BOOLEAN NOT NULL DEFAULT FALSE
);
```

### Configuration

```
COMPLIANCE_SAR_MIN_SCORE=70
COMPLIANCE_INSTITUTION_NAME=               # required; reject startup if blank
COMPLIANCE_EXPORT_RATE_LIMIT_PER_HOUR=100
```

## Security Considerations

- **PII handling**: originator/beneficiary names in Travel Rule records are PII. They must never be logged, echoed in error messages, or stored in the audit log. The audit log stores only the wallet hash, not plaintext addresses or names
- **Wallet address in SAR narratives**: Stellar public keys are pseudonymous (not personal data under most jurisdictions) but should still be truncated to `GXXXX...YYYY` in log entries. The full address appears only in the returned document body
- **Admin key requirement**: both export endpoints require `X-LedgerLens-Admin-Key`. Return 503 (not 401) if the key is unset
- **SAR narrative injection**: the `institution_name` field is caller-supplied and appears in the narrative text. Sanitise it: strip HTML tags, limit to 200 characters, allow only printable ASCII. Reject and return 422 for anything outside this range
- **Rate limiting**: SAR export can be expensive (triggers SHAP recomputation). Rate-limit to 100 exports/hour per admin key. Track in SQLite `compliance_exports` table (count rows in last hour)
- **Dry-run audit gap**: dry-run exports do not write to the audit log — document this limitation clearly. Compliance officers must not use dry-run for regulatory-submission versions

## Testing Requirements

- [ ] `tests/test_compliance_exporter.py`
- [ ] Test: `SARNarrativeBuilder.build` produces narrative text containing wallet (truncated), risk score, Benford summary, and ring summary
- [ ] Test: narrative for a wallet with no ring detected produces empty ring summary (not "None" or error text)
- [ ] Test: SHAP top-3 produces 3 bullet points in correct format
- [ ] Test: `TravelRuleRecord.to_json()` produces IVMS101-compatible structure with all required fields
- [ ] Test: `POST /compliance/export/sar` with score < 70 returns 400
- [ ] Test: `POST /compliance/export/sar` with `dry_run=true` does not write to audit log
- [ ] Test: `institution_name` with HTML injection chars → 422 response
- [ ] Test: rate limit — 101st request in one hour returns 429
- [ ] Test: audit log stores wallet_hash (not plaintext) and dry_run flag

## Documentation Requirements

- [ ] Docstrings on `SARNarrativeBuilder`, `TravelRuleRecordBuilder`, `ComplianceExporter`
- [ ] Add `docs/compliance_export.md` covering: regulatory context (FATF R16, FinCEN SAR), how to use the export API, field mapping to FinCEN SAR form sections, IVMS101 schema reference, legal disclaimer (LedgerLens is a tool, not a compliance advisor)
- [ ] Update `README.md` to mention the compliance export pipeline
- [ ] Document `compliance_exports` table in `docs/database_schema.md`
- [ ] Update `.env.example` with three new configuration variables including the mandatory `COMPLIANCE_INSTITUTION_NAME`

## Definition of Done

- [ ] `SARNarrativeBuilder` and `TravelRuleRecordBuilder` implemented
- [ ] Both export API endpoints live with admin key and rate limiting
- [ ] Audit log stores wallet hash (not plaintext), export type, and dry_run flag
- [ ] `POST /compliance/export/sar` returns 400 for score < 70
- [ ] SAR narrative contains all six required elements (wallet, score, CI, Benford, ring, SHAP)
- [ ] All tests pass
- [ ] `docs/compliance_export.md` with legal disclaimer authored

## For Contributors

**Ideal contributor profile**: You have experience building compliance-adjacent systems in fintech or blockchain — AML/CFT pipelines, SAR generation tools, or VASP Travel Rule implementations. You understand FATF Recommendation 16, the IVMS101 messaging standard, and FinCEN SAR form structure. Comfort with data privacy considerations (pseudonymous vs personal data, PII handling) is essential. Experience with Python templating systems (Jinja2 or string formatting) and rate-limiting in FastAPI is helpful.

To apply, please comment on this issue stating:

1. **Specialty area** — e.g., "AML/CFT compliance systems", "FATF Travel Rule / IVMS101", "FinCEN SAR automation"
2. **Relevant experience** — compliance tooling you have built; VASP or exchange compliance integrations; relevant regulatory knowledge
3. **Approach / initial thoughts** — your thoughts on the SAR narrative template approach vs a structured data model; concerns about PII handling in the Travel Rule record; jurisdiction-specific variations you would want to accommodate
4. **Estimated time** — breakdown by component (SAR builder, Travel Rule builder, API, audit log, tests, docs)
