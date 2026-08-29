#!/usr/bin/env python3
"""Enforce security/vulnerability-waivers.yml against a scanner's JSON report.

Grand 5 / #702, Required scope B: restores blocking behavior to the
dependency scanners in .github/workflows/license-vuln-scan.yml, replacing
the blanket ``|| true`` that made every one of them purely informational
(git show d420803, "Make vulnerability scans report-only", 2026-08-06) with
a single, consistent, auditable waiver mechanism across all four
ecosystems.

Design decision — why a fail-closed default on *unknown* severity: several
of the four scanners this reads (osv-scanner for advisories with no CVSS
score, cargo-audit for RUSTSEC advisories with no severity field) can
report a real vulnerability with no machine-readable severity at all. This
script treats "severity we could not determine" the same as HIGH rather
than silently passing it through — an under-classified finding is exactly
the kind of thing a blanket ``|| true`` was already failing to catch before
this script existed; repeating that mistake via a different code path
(quietly skipping anything the severity parser doesn't recognize) would
defeat the point. If a specific unknown-severity finding turns out to be
genuinely low-risk after review, waive it explicitly — that decision then
lives in security/vulnerability-waivers.yml with a reason, not in this
script's parsing logic.

Usage:
    check_vuln_waivers.py --scanner osv --report /path/to/osv.json
    check_vuln_waivers.py --scanner cargo-audit --report /path/to/audit.json
    check_vuln_waivers.py --scanner govulncheck --report /path/to/govulncheck.ndjson
    check_vuln_waivers.py --scanner npm-audit --report /path/to/npm-audit.json

Exit code 0: no unwaived CRITICAL/HIGH finding. Exit code 1: at least one
unwaived (or waived-but-expired) CRITICAL/HIGH finding — the calling CI
job must not swallow this with ``|| true``.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard, see requirements/test.in
    print(
        "FATAL: PyYAML is required (pip install pyyaml) — it is already a "
        "transitive dependency of this project's own tooling; if this "
        "script is invoked outside that environment, install it explicitly.",
        file=sys.stderr,
    )
    raise

BLOCKING_SEVERITIES = {"CRITICAL", "HIGH"}


@dataclass(frozen=True)
class Finding:
    id: str
    ecosystem: str
    package: str
    severity: str  # normalized to one of CRITICAL/HIGH/MODERATE/LOW/UNKNOWN


@dataclass(frozen=True)
class Waiver:
    id: str
    ecosystem: str
    reason: str
    expires: datetime.date


def load_waivers(path: Path) -> dict[tuple[str, str], Waiver]:
    """Load security/vulnerability-waivers.yml into an (id, ecosystem) -> Waiver map.

    Keyed on (id, ecosystem) rather than id alone — see that file's schema
    comment: the same identifier string must never accidentally waive a
    finding in an unrelated ecosystem.
    """
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    entries = data.get("waivers") or []
    waivers: dict[tuple[str, str], Waiver] = {}
    for entry in entries:
        missing = [f for f in ("id", "ecosystem", "reason", "expires") if not entry.get(f)]
        if missing:
            raise ValueError(
                f"security/vulnerability-waivers.yml entry {entry!r} is missing "
                f"required field(s): {', '.join(missing)}"
            )
        expires = entry["expires"]
        if isinstance(expires, str):
            expires = datetime.date.fromisoformat(expires)
        elif isinstance(expires, datetime.datetime):
            expires = expires.date()
        # else: PyYAML already parsed a bare YAML date scalar into a
        # datetime.date — used as-is.
        key = (str(entry["id"]), str(entry["ecosystem"]))
        waivers[key] = Waiver(
            id=str(entry["id"]),
            ecosystem=str(entry["ecosystem"]),
            reason=str(entry["reason"]),
            expires=expires,
        )
    return waivers


def _severity_from_cvss_score(score: float) -> str:
    # CVSS v3 qualitative severity rating bands, per the FIRST.org spec —
    # the same bands Trivy/most scanners use, so a numeric-only advisory is
    # bucketed consistently with anything that already reports a
    # qualitative severity string directly.
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MODERATE"
    if score > 0.0:
        return "LOW"
    return "UNKNOWN"


def parse_osv(report: dict) -> list[Finding]:
    """Parse `osv-scanner --format json` output.

    Schema: {"results": [{"packages": [{"package": {...}, "vulnerabilities": [...]}]}]}
    Each vulnerability may carry `database_specific.severity` (a clean
    qualitative string on GHSA-sourced advisories) or a `severity` array of
    {"type": "CVSS_V3", "score": "<vector or number>"} objects instead —
    the two are not both always present, so both are checked.
    """
    findings: list[Finding] = []
    for result in report.get("results", []):
        for pkg in result.get("packages", []):
            package_name = pkg.get("package", {}).get("name", "<unknown>")
            for vuln in pkg.get("vulnerabilities", []):
                vuln_id = vuln.get("id", "<unknown-id>")
                severity = "UNKNOWN"
                db_specific = vuln.get("database_specific", {}) or {}
                if isinstance(db_specific.get("severity"), str):
                    severity = db_specific["severity"].upper()
                if severity == "UNKNOWN":
                    for sev_entry in vuln.get("severity", []) or []:
                        score_raw = sev_entry.get("score", "")
                        # CVSS vector strings ("CVSS:3.1/AV:N/...") don't
                        # carry a bare numeric score; only attempt the
                        # numeric bucketing when the field really is one.
                        try:
                            severity = _severity_from_cvss_score(float(score_raw))
                            break
                        except (TypeError, ValueError):
                            continue
                findings.append(
                    Finding(id=vuln_id, ecosystem="python", package=package_name, severity=severity)
                )
    return findings


def parse_cargo_audit(report: dict) -> list[Finding]:
    """Parse `cargo audit --json` output.

    Schema: {"vulnerabilities": {"list": [{"advisory": {"id": "RUSTSEC-...",
    "cvss": "<vector or null>", ...}, "package": {"name": "..."}}]}}
    `advisory.cvss` is a CVSS vector *string* when present (e.g.
    "CVSS:3.1/AV:N/AC:L/..."), not a bare score — cargo-audit does not
    compute a numeric score itself, so unlike OSV there's no reliable
    number to bucket here. Treated as UNKNOWN (-> blocking, per this
    script's fail-closed default) unless a future cargo-audit version adds
    one; this is the correct conservative choice given RUSTSEC advisories
    routinely represent exploitable memory-safety issues in Rust crates.
    """
    findings: list[Finding] = []
    for entry in report.get("vulnerabilities", {}).get("list", []) or []:
        advisory = entry.get("advisory", {})
        package = entry.get("package", {}).get("name", "<unknown>")
        findings.append(
            Finding(id=advisory.get("id", "<unknown-id>"), ecosystem="rust", package=package, severity="UNKNOWN")
        )
    return findings


def _iter_json_values(text: str):
    """Yield successive top-level JSON values concatenated in `text`.

    govulncheck's `-json` output is NOT newline-delimited JSON (confirmed
    against real `govulncheck -json` v1.7.0 output, not assumed): each
    message is written *pretty-printed* (multi-line, indented) and messages
    are concatenated back-to-back with no separator at all — not even a
    blank line. Splitting on "\\n" and json.loads()-ing each line therefore
    breaks on the very first message, since most lines are JSON fragments
    like `"osv": {` rather than complete values. A streaming decoder that
    walks the whole text and repeatedly raw_decodes the next value,
    skipping the whitespace between them, handles this format regardless
    of whether a given govulncheck version pretty-prints or compacts it.
    """
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        obj, end = decoder.raw_decode(text, idx)
        yield obj
        idx = end


def parse_govulncheck(text: str) -> list[Finding]:
    """Parse `govulncheck -json` output.

    govulncheck's real value over a plain vulnerability-database lookup is
    distinguishing "this vulnerable package is a dependency" from "your
    code actually calls the vulnerable function" — it emits an `osv` object
    per known vulnerability in the dependency graph, and separately a
    `finding` object per *actually reachable* call path (module/version-
    level for a vulnerable stdlib/toolchain, or symbol-level for an
    imported package — govulncheck decides that granularity itself; this
    script just trusts that a `finding` message with a non-empty `trace`
    means govulncheck judged it reachable). Only the reachable ones are
    worth blocking a build over; the whole point of running govulncheck
    instead of a generic Go-module vuln scan is to avoid failing builds
    over vulnerable code paths the binary never executes. Findings are
    still not skipped silently if their severity can't be read from the
    OSV record — see the module fail-closed note — it's specifically
    *reachability*, not severity, that's used to narrow the set here.
    """
    osv_by_id: dict[str, dict] = {}
    reachable_ids: set[str] = set()
    for obj in _iter_json_values(text):
        if not isinstance(obj, dict):
            continue
        if "osv" in obj and isinstance(obj["osv"], dict):
            osv = obj["osv"]
            osv_by_id[osv.get("id", "<unknown-id>")] = osv
        elif "finding" in obj:
            finding = obj["finding"]
            osv_id = finding.get("osv")
            if osv_id and finding.get("trace"):
                reachable_ids.add(osv_id)

    findings: list[Finding] = []
    for vuln_id in reachable_ids:
        osv = osv_by_id.get(vuln_id, {})
        severity = "UNKNOWN"
        db_specific = osv.get("database_specific", {}) or {}
        if isinstance(db_specific.get("severity"), str):
            severity = db_specific["severity"].upper()
        package = "<unknown>"
        affected = osv.get("affected", []) or []
        if affected:
            package = affected[0].get("package", {}).get("name", "<unknown>")
        findings.append(Finding(id=vuln_id, ecosystem="go", package=package, severity=severity))
    return findings


def parse_npm_audit(report: dict) -> list[Finding]:
    """Parse `npm audit --json` output (npm >= 7).

    Schema: {"vulnerabilities": {"<package-name>": {"severity": "...",
    "via": [<advisory object or dependency-name string>, ...]}}}. `via`
    mixes advisory objects (a real, distinct finding — has a `url` like
    ".../advisories/GHSA-xxxx-xxxx-xxxx" or a `source` numeric id) with
    bare strings (a transitive reference to *another key* in this same
    `vulnerabilities` map, not a separate finding) — only the objects are
    counted here to avoid double-reporting the same underlying advisory
    once per package in its dependency chain.
    """
    findings: list[Finding] = []
    for package, entry in report.get("vulnerabilities", {}).items():
        severity = str(entry.get("severity", "unknown")).upper()
        for via in entry.get("via", []) or []:
            if not isinstance(via, dict):
                continue  # bare string = reference to another map key, not a finding
            advisory_id = via.get("url", "").rsplit("/", 1)[-1] or f"npm-source-{via.get('source', '?')}"
            findings.append(Finding(id=advisory_id, ecosystem="npm", package=package, severity=severity))
    return findings


def evaluate(
    findings: list[Finding], waivers: dict[tuple[str, str], Waiver], today: datetime.date
) -> tuple[bool, list[str]]:
    """Returns (ok, report_lines). ok is False iff any BLOCKING_SEVERITIES
    finding is unwaived or its waiver has expired."""
    ok = True
    lines: list[str] = []
    blocking = [f for f in findings if f.severity in BLOCKING_SEVERITIES or f.severity == "UNKNOWN"]

    if not findings:
        lines.append("No findings reported by the scanner.")
        return True, lines

    non_blocking = len(findings) - len(blocking)
    if non_blocking:
        lines.append(f"{non_blocking} finding(s) below CRITICAL/HIGH — not evaluated against waivers.")

    for f in blocking:
        waiver = waivers.get((f.id, f.ecosystem))
        if waiver is None:
            ok = False
            lines.append(f"BLOCK  [{f.severity:8}] {f.id} in {f.package} ({f.ecosystem}) — no waiver on file.")
            continue
        if waiver.expires < today:
            ok = False
            lines.append(
                f"BLOCK  [{f.severity:8}] {f.id} in {f.package} ({f.ecosystem}) — "
                f"waiver EXPIRED on {waiver.expires.isoformat()} (was: {waiver.reason!r}). "
                f"Renew the entry in security/vulnerability-waivers.yml if still accepted, or fix the dependency."
            )
            continue
        lines.append(
            f"WAIVED [{f.severity:8}] {f.id} in {f.package} ({f.ecosystem}) — "
            f"expires {waiver.expires.isoformat()}. Reason: {waiver.reason}"
        )

    return ok, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scanner", required=True, choices=["osv", "cargo-audit", "govulncheck", "npm-audit"])
    parser.add_argument("--report", required=True, type=Path, help="Path to the scanner's JSON/NDJSON output")
    parser.add_argument(
        "--waivers",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "security" / "vulnerability-waivers.yml",
    )
    args = parser.parse_args()

    if not args.report.exists():
        print(f"FATAL: report file not found: {args.report}", file=sys.stderr)
        return 2

    if args.scanner == "govulncheck":
        findings = parse_govulncheck(args.report.read_text())
    else:
        report = json.loads(args.report.read_text())
        findings = {
            "osv": parse_osv,
            "cargo-audit": parse_cargo_audit,
            "npm-audit": parse_npm_audit,
        }[args.scanner](report)

    waivers = load_waivers(args.waivers)
    ok, lines = evaluate(findings, waivers, datetime.date.today())

    print(f"── {args.scanner} vulnerability gate ──────────────────────────")
    for line in lines:
        print(line)
    print()
    if ok:
        print(f"PASS — no unwaived CRITICAL/HIGH findings ({len(findings)} total finding(s) reviewed).")
        return 0
    print("FAIL — unwaived or expired-waiver CRITICAL/HIGH finding(s) present (see BLOCK lines above).")
    print("To accept a finding, add a dated, justified entry to security/vulnerability-waivers.yml.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
