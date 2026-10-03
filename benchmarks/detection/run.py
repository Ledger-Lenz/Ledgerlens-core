"""Run the detection pipeline against the labeled benchmark and diff vs. baseline.

Usage:
    python -m benchmarks.detection.run                  # report + check
    python -m benchmarks.detection.run --update-baseline

Exits non-zero if any metric drops more than ``--tolerance`` below baseline.
Writes a Markdown delta table to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

from benchmarks.detection.dataset import DATA_DIR, DATASET_VERSION, load
from detection.feature_engineering import self_matching_rate
from detection.graph_engine import build_transaction_graph, find_wash_rings


def detect(trades: pd.DataFrame) -> set[str]:
    """Accounts flagged by graph ring detection or self-matching."""
    flagged: set[str] = set()
    for ring in find_wash_rings(build_transaction_graph(trades)):
        flagged.update(ring["accounts"])
    for acct, group in trades.groupby("base_account"):
        if self_matching_rate(group) > 0:
            flagged.add(acct)
    return flagged


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4)}


def evaluate(trades: pd.DataFrame, labels: pd.DataFrame) -> dict:
    flagged = detect(trades)
    positives = set(labels.loc[labels.label == 1, "account"])
    tp, fp, fn = len(flagged & positives), len(flagged - positives), len(positives - flagged)
    metrics = {"overall": _prf(tp, fp, fn)}
    for pattern, group in labels[labels.label == 1].groupby("pattern"):
        accts = set(group.account)
        metrics[pattern] = {"recall": round(len(flagged & accts) / len(accts), 4)}
    return metrics


def _deltas(current: dict, baseline: dict) -> list[tuple[str, str, float, float]]:
    out = []
    for scope, vals in current.items():
        for metric, value in vals.items():
            out.append((scope, metric, baseline.get(scope, {}).get(metric, 0.0), value))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DATASET_VERSION)
    parser.add_argument("--tolerance", type=float, default=0.02)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args(argv)

    baseline_path = DATA_DIR / args.version / "baseline.json"
    current = evaluate(*load(args.version))
    if args.update_baseline:
        baseline_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {baseline_path}")
        return 0

    baseline = json.loads(baseline_path.read_text())
    rows = _deltas(current, baseline)
    lines = [
        f"### Detection benchmark ({args.version})",
        "",
        "| scope | metric | baseline | current | delta |",
        "|---|---|---|---|---|",
    ]
    regressions = []
    for scope, metric, base, cur in rows:
        delta = cur - base
        lines.append(f"| {scope} | {metric} | {base:.4f} | {cur:.4f} | {delta:+.4f} |")
        if delta < -args.tolerance:
            regressions.append(f"{scope}.{metric}")
    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as fh:
            fh.write(report + "\n")
    if regressions:
        print(
            f"REGRESSION beyond tolerance {args.tolerance}: {', '.join(regressions)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
