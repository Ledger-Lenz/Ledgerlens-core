"""Verify every STRIDE threat in docs/threat_model.md is traced to a test.

Fails (exit 1) when a threat has no row in docs/threat_test_matrix.md, a
referenced test file/function does not exist, or a ``gap`` row lacks a
follow-up tracked in TODO.md. See docs/threat_test_matrix.md.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THREAT_MODEL = ROOT / "docs" / "threat_model.md"
MATRIX = ROOT / "docs" / "threat_test_matrix.md"
TODO = ROOT / "TODO.md"

_BOUNDARY_RE = re.compile(r"^### Boundary (\d+):")
_STRIDE_ROW_RE = re.compile(r"^\| \*\*([STRIDE])\*\*")
_MATRIX_ROW_RE = re.compile(r"^\| (B\d+-[STRIDE]) \|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|")
_REF_RE = re.compile(r"`([^`]+)`")


def threat_ids() -> list[str]:
    ids, boundary = [], None
    for line in THREAT_MODEL.read_text().splitlines():
        if m := _BOUNDARY_RE.match(line):
            boundary = m.group(1)
        elif line.startswith("## "):
            boundary = None
        elif boundary and (m := _STRIDE_ROW_RE.match(line)):
            ids.append(f"B{boundary}-{m.group(1)}")
    return ids


def ref_error(ref: str) -> str | None:
    path, _, name = ref.partition("::")
    file = ROOT / path
    if not file.is_file():
        return f"file not found: {path}"
    if name and not re.search(rf"\b(?:def|fn) {re.escape(name)}\b", file.read_text()):
        return f"test not found: {ref}"
    return None


def main() -> int:
    errors: list[str] = []
    rows: dict[str, tuple[list[str], str, str]] = {}
    for line in MATRIX.read_text().splitlines():
        if m := _MATRIX_ROW_RE.match(line):
            tid, _, tests, status, follow_up = (g.strip() for g in m.groups())
            rows[tid] = (_REF_RE.findall(tests), status, follow_up)

    todo = TODO.read_text()
    for tid in threat_ids():
        if tid not in rows:
            errors.append(f"{tid}: no row in {MATRIX.relative_to(ROOT)}")
            continue
        refs, status, follow_up = rows[tid]
        if status == "covered":
            if not refs:
                errors.append(f"{tid}: marked covered but lists no test")
            errors.extend(f"{tid}: {e}" for r in refs if (e := ref_error(r)))
        elif status == "gap":
            if not follow_up or f"[{follow_up}]" not in todo:
                errors.append(f"{tid}: gap without a follow-up tracked in TODO.md")
        else:
            errors.append(f"{tid}: unknown status {status!r} (use covered/gap)")

    for e in errors:
        print(f"::error::{e}")
    print(f"Threat matrix: {len(rows)} rows, {len(errors)} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
