---
title: "Implement Regulatory Explainability Report Generator for Audit Submissions"
labels: ["difficulty: advanced", "area: compliance", "type: feature"]
assignees: []
---

## Summary
Regulators and exchange compliance teams conducting audits require a complete, human-readable report explaining why a specific wallet received a high risk score on a given date — including the feature values, SHAP attributions, model version, and data provenance. A report generator that produces a self-contained PDF or HTML audit report from a wallet address and date enables compliance teams to respond to regulator inquiries with minimal manual effort.

## Objectives
- [ ] Implement `ComplianceReportGenerator` in `detection/compliance_report.py`
- [ ] Report sections: executive summary, risk score with CI, top-5 SHAP features with plain-English descriptions, Benford analysis chart, trade timeline, model version and training date, data provenance (Horizon cursor range)
- [ ] Output: HTML (via Jinja2 template) and optionally PDF (via `weasyprint`)
- [ ] `cli.py report generate --wallet G... --date YYYY-MM-DD --output report.html`
- [ ] Reports are read-only and idempotent: same wallet+date always produces the same report

## Definition of Done
- [ ] HTML report renders correctly in Chrome and Firefox
- [ ] PDF output passes PDF/A-1b validation for archival compliance
- [ ] All six report sections present with no placeholder text
- [ ] Report includes LedgerLens version hash and model signature for auditability
