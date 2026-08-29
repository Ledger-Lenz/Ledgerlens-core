"""Tests for scripts/check_vuln_waivers.py (Grand 5 / #702, Required scope B).

Fixtures below mirror each scanner's *real* output shape (osv-scanner
--format json, cargo audit --json, govulncheck -json, npm audit --json) —
trimmed to the fields the parser actually reads, but structurally accurate,
not simplified into a shape that happens to be convenient to parse. See
each parse_* docstring in check_vuln_waivers.py for the schema notes these
fixtures are exercising.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import check_vuln_waivers as cvw  # noqa: E402


TODAY = datetime.date(2026, 8, 29)


# ── osv-scanner ──────────────────────────────────────────────────────────────

def test_parse_osv_severity_from_database_specific():
    report = {
        "results": [
            {
                "packages": [
                    {
                        "package": {"name": "requests", "version": "2.25.0", "ecosystem": "PyPI"},
                        "vulnerabilities": [
                            {
                                "id": "GHSA-abcd-1234-efgh",
                                "database_specific": {"severity": "HIGH"},
                            }
                        ],
                    }
                ]
            }
        ]
    }
    findings = cvw.parse_osv(report)
    assert findings == [cvw.Finding(id="GHSA-abcd-1234-efgh", ecosystem="python", package="requests", severity="HIGH")]


def test_parse_osv_severity_from_cvss_score_bucketing():
    report = {
        "results": [
            {
                "packages": [
                    {
                        "package": {"name": "pyyaml"},
                        "vulnerabilities": [
                            {
                                "id": "CVE-2024-99999",
                                "severity": [{"type": "CVSS_V3", "score": "9.8"}],
                            }
                        ],
                    }
                ]
            }
        ]
    }
    [finding] = cvw.parse_osv(report)
    assert finding.severity == "CRITICAL"


def test_parse_osv_cvss_vector_string_is_not_misparsed_as_number():
    # A CVSS *vector* string ("CVSS:3.1/AV:N/...") must not crash float()
    # or be silently treated as a valid score — this is the exact kind of
    # malformed-looking-but-real-world input the parser has to tolerate.
    report = {
        "results": [
            {
                "packages": [
                    {
                        "package": {"name": "somepkg"},
                        "vulnerabilities": [
                            {
                                "id": "GHSA-vector-only",
                                "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                            }
                        ],
                    }
                ]
            }
        ]
    }
    [finding] = cvw.parse_osv(report)
    assert finding.severity == "UNKNOWN"


def test_parse_osv_no_findings():
    assert cvw.parse_osv({"results": []}) == []


# ── cargo-audit ──────────────────────────────────────────────────────────────

def test_parse_cargo_audit_always_unknown_severity():
    report = {
        "vulnerabilities": {
            "list": [
                {
                    "advisory": {"id": "RUSTSEC-2024-0001", "cvss": None},
                    "package": {"name": "some-crate", "version": "0.1.0"},
                }
            ]
        }
    }
    findings = cvw.parse_cargo_audit(report)
    assert findings == [cvw.Finding(id="RUSTSEC-2024-0001", ecosystem="rust", package="some-crate", severity="UNKNOWN")]


def test_parse_cargo_audit_no_vulnerabilities():
    assert cvw.parse_cargo_audit({"vulnerabilities": {"list": []}}) == []


# ── govulncheck ──────────────────────────────────────────────────────────────

def test_parse_govulncheck_only_reachable_findings_count():
    lines = [
        json.dumps({"osv": {"id": "GO-2024-0001", "database_specific": {"severity": "HIGH"}, "affected": [{"package": {"name": "golang.org/x/net"}}]}}),
        json.dumps({"osv": {"id": "GO-2024-0002", "database_specific": {"severity": "CRITICAL"}, "affected": [{"package": {"name": "some/other"}}]}}),
        # Only GO-2024-0001 has an actual reachable call trace.
        json.dumps({"finding": {"osv": "GO-2024-0001", "trace": [{"function": "Foo"}]}}),
    ]
    findings = cvw.parse_govulncheck(lines)
    assert len(findings) == 1
    assert findings[0].id == "GO-2024-0001"
    assert findings[0].severity == "HIGH"


def test_parse_govulncheck_finding_without_trace_is_not_reachable():
    lines = [
        json.dumps({"osv": {"id": "GO-2024-0003", "database_specific": {"severity": "HIGH"}}}),
        # A finding entry with no/empty trace = not actually called.
        json.dumps({"finding": {"osv": "GO-2024-0003", "trace": []}}),
    ]
    assert cvw.parse_govulncheck(lines) == []


def test_parse_govulncheck_ignores_progress_lines():
    lines = [
        json.dumps({"progress": {"message": "Scanning your code and 42 packages..."}}),
        "",  # blank lines are common in real NDJSON output
    ]
    assert cvw.parse_govulncheck(lines) == []


# ── npm audit ────────────────────────────────────────────────────────────────

def test_parse_npm_audit_counts_advisory_objects_not_string_refs():
    report = {
        "vulnerabilities": {
            "lodash": {
                "severity": "high",
                "via": [
                    {"source": 1234, "url": "https://github.com/advisories/GHSA-jf85-cpcp-j695", "title": "Prototype Pollution"},
                    "some-transitive-parent",  # bare string = not a distinct finding
                ],
            }
        }
    }
    findings = cvw.parse_npm_audit(report)
    assert findings == [cvw.Finding(id="GHSA-jf85-cpcp-j695", ecosystem="npm", package="lodash", severity="HIGH")]


def test_parse_npm_audit_no_vulnerabilities():
    assert cvw.parse_npm_audit({"vulnerabilities": {}}) == []


# ── waiver loading ───────────────────────────────────────────────────────────

def test_load_waivers_missing_file_returns_empty(tmp_path):
    assert cvw.load_waivers(tmp_path / "does-not-exist.yml") == {}


def test_load_waivers_parses_valid_entries(tmp_path):
    waivers_file = tmp_path / "waivers.yml"
    waivers_file.write_text(
        """
waivers:
  - id: RUSTSEC-2024-0001
    ecosystem: rust
    package: some-crate
    reason: "Not reachable in our build (feature disabled)."
    expires: 2099-12-31
    added_by: octocat
    added_on: 2026-08-29
"""
    )
    waivers = cvw.load_waivers(waivers_file)
    assert (("RUSTSEC-2024-0001", "rust")) in waivers
    w = waivers[("RUSTSEC-2024-0001", "rust")]
    assert w.expires == datetime.date(2099, 12, 31)
    assert w.reason == "Not reachable in our build (feature disabled)."


def test_load_waivers_missing_required_field_raises(tmp_path):
    waivers_file = tmp_path / "waivers.yml"
    waivers_file.write_text(
        """
waivers:
  - id: RUSTSEC-2024-0001
    ecosystem: rust
    # missing reason and expires
"""
    )
    with pytest.raises(ValueError, match="missing required field"):
        cvw.load_waivers(waivers_file)


def test_load_waivers_empty_list(tmp_path):
    waivers_file = tmp_path / "waivers.yml"
    waivers_file.write_text("waivers: []\n")
    assert cvw.load_waivers(waivers_file) == {}


# ── evaluate() — the actual gating decision ─────────────────────────────────

def test_evaluate_no_findings_passes():
    ok, lines = cvw.evaluate([], {}, TODAY)
    assert ok is True


def test_evaluate_unwaived_critical_blocks():
    findings = [cvw.Finding(id="CVE-1", ecosystem="python", package="pkg", severity="CRITICAL")]
    ok, lines = cvw.evaluate(findings, {}, TODAY)
    assert ok is False
    assert any("BLOCK" in line and "no waiver on file" in line for line in lines)


def test_evaluate_unknown_severity_blocks_by_default():
    # The fail-closed default this module documents explicitly: an
    # unclassified finding (e.g. every cargo-audit result) must still
    # block, not silently pass.
    findings = [cvw.Finding(id="RUSTSEC-X", ecosystem="rust", package="pkg", severity="UNKNOWN")]
    ok, _ = cvw.evaluate(findings, {}, TODAY)
    assert ok is False


def test_evaluate_moderate_severity_does_not_block():
    findings = [cvw.Finding(id="CVE-2", ecosystem="python", package="pkg", severity="MODERATE")]
    ok, lines = cvw.evaluate(findings, {}, TODAY)
    assert ok is True
    assert any("not evaluated against waivers" in line for line in lines)


def test_evaluate_valid_waiver_passes_and_is_logged():
    findings = [cvw.Finding(id="CVE-3", ecosystem="python", package="pkg", severity="HIGH")]
    waivers = {
        ("CVE-3", "python"): cvw.Waiver(
            id="CVE-3", ecosystem="python", reason="Test reason", expires=datetime.date(2099, 1, 1)
        )
    }
    ok, lines = cvw.evaluate(findings, waivers, TODAY)
    assert ok is True
    assert any("WAIVED" in line and "Test reason" in line for line in lines)


def test_evaluate_expired_waiver_blocks_and_says_why():
    findings = [cvw.Finding(id="CVE-4", ecosystem="python", package="pkg", severity="CRITICAL")]
    waivers = {
        ("CVE-4", "python"): cvw.Waiver(
            id="CVE-4", ecosystem="python", reason="Old reason", expires=datetime.date(2020, 1, 1)
        )
    }
    ok, lines = cvw.evaluate(findings, waivers, TODAY)
    assert ok is False
    assert any("EXPIRED" in line for line in lines)


def test_evaluate_waiver_scoped_to_ecosystem_does_not_leak():
    # Same id, different ecosystem than the waiver -> must still block.
    # This is the specific cross-ecosystem-collision protection the
    # (id, ecosystem) composite key in load_waivers()/evaluate() exists for.
    findings = [cvw.Finding(id="CVE-5", ecosystem="npm", package="pkg", severity="HIGH")]
    waivers = {
        ("CVE-5", "python"): cvw.Waiver(
            id="CVE-5", ecosystem="python", reason="Waived for Python only", expires=datetime.date(2099, 1, 1)
        )
    }
    ok, _ = cvw.evaluate(findings, waivers, TODAY)
    assert ok is False


def test_evaluate_waiver_expiring_today_is_still_valid():
    # Boundary: a waiver expiring exactly "today" has not yet expired.
    findings = [cvw.Finding(id="CVE-6", ecosystem="go", package="pkg", severity="HIGH")]
    waivers = {("CVE-6", "go"): cvw.Waiver(id="CVE-6", ecosystem="go", reason="r", expires=TODAY)}
    ok, _ = cvw.evaluate(findings, waivers, TODAY)
    assert ok is True


def test_evaluate_waiver_expired_yesterday_blocks():
    findings = [cvw.Finding(id="CVE-7", ecosystem="go", package="pkg", severity="HIGH")]
    waivers = {
        ("CVE-7", "go"): cvw.Waiver(id="CVE-7", ecosystem="go", reason="r", expires=TODAY - datetime.timedelta(days=1))
    }
    ok, _ = cvw.evaluate(findings, waivers, TODAY)
    assert ok is False
