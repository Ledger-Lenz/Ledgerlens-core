"""Triage and deduplicate fuzz crashes into the regression corpus.

Replays every crash/timeout/oom input found under ``--artifacts-dir`` against
its harness, derives a stable signature from the resulting Python traceback
(exception type + innermost in-repo frames, line numbers stripped), and keeps
only the smallest input per signature. Unique reproducers are copied into
``fuzz/regression/<harness>/`` and replayed by
``tests/test_fuzz_harness_smoke.py`` on every CI run.

Usage:
    python scripts/fuzz_triage.py --artifacts-dir fuzz/artifacts \
        --regression-dir fuzz/regression --report fuzz/triage_report.json

Expected layout: ``<artifacts-dir>/<harness-name>/{crash,timeout,oom}-*``.
Exits 1 when at least one *new* unique crash signature was found.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in (\S+)')
_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)\b)", re.M)
_FRAMES_IN_SIGNATURE = 3
_REPLAY_TIMEOUT_S = 60


def signature(kind: str, output: str) -> str:
    """Return a stable crash signature from harness stderr/stdout."""
    frames = [
        f"{Path(path).name}:{func}"
        for path, func in _FRAME_RE.findall(output)
        if "site-packages" not in path and "atheris" not in path
    ]
    exc_matches = _EXC_RE.findall(output)
    exc = exc_matches[-1] if exc_matches else kind
    material = "|".join([exc, *frames[-_FRAMES_IN_SIGNATURE:]])
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def replay(harness: Path, crash: Path) -> str:
    """Run one crash input through its harness and return combined output."""
    try:
        proc = subprocess.run(
            [sys.executable, str(harness), str(crash)],
            capture_output=True,
            text=True,
            timeout=_REPLAY_TIMEOUT_S,
            cwd=REPO_ROOT,
        )
        return proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return "TimeoutExpired"


def triage(artifacts_dir: Path, regression_dir: Path) -> dict:
    report: dict = {"new": [], "known": [], "duplicates": 0}
    for harness_dir in sorted(p for p in artifacts_dir.iterdir() if p.is_dir()):
        name = harness_dir.name
        harness = REPO_ROOT / "fuzz" / f"{name}.py"
        if not harness.exists():
            continue
        dest = regression_dir / name
        dest.mkdir(parents=True, exist_ok=True)
        known = {p.stem for p in dest.iterdir() if p.suffix == ".bin"}

        best: dict[str, Path] = {}
        crashes = sorted(
            p for p in harness_dir.iterdir()
            if p.is_file() and p.name.split("-", 1)[0] in {"crash", "timeout", "oom"}
        )
        for crash in crashes:
            sig = signature(crash.name.split("-", 1)[0], replay(harness, crash))
            if sig in best:
                report["duplicates"] += 1
                if crash.stat().st_size < best[sig].stat().st_size:
                    best[sig] = crash
            else:
                best[sig] = crash

        for sig, crash in best.items():
            entry = {"harness": name, "signature": sig, "input": crash.name}
            if sig in known:
                report["known"].append(entry)
                continue
            (dest / f"{sig}.bin").write_bytes(crash.read_bytes())
            report["new"].append(entry)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts-dir", type=Path, default=Path("fuzz/artifacts"))
    parser.add_argument("--regression-dir", type=Path, default=Path("fuzz/regression"))
    parser.add_argument("--report", type=Path, default=Path("fuzz/triage_report.json"))
    args = parser.parse_args()

    if not args.artifacts_dir.is_dir():
        print(f"No artifacts directory at {args.artifacts_dir}; nothing to triage.")
        report: dict = {"new": [], "known": [], "duplicates": 0}
    else:
        report = triage(args.artifacts_dir, args.regression_dir)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Triage: {len(report['new'])} new, {len(report['known'])} known, "
        f"{report['duplicates']} duplicates suppressed."
    )
    return 1 if report["new"] else 0


if __name__ == "__main__":
    sys.exit(main())
