"""Run the full chaos-mesh experiment suite and publish a resilience scorecard.

Usage
-----
    python chaos-mesh/run_chaos_day.py \
        --health-url https://ledgerlens.staging.example/health \
        --output-dir scorecard \
        --previous previous/scorecard.json

For every experiment registered in ``EXPERIMENTS`` this script applies the
YAML, waits for the fault to be injected, runs the same checks as
``verify_experiment.py`` (degraded-mode assertion where one is defined, then
recovery), and always deletes the experiment afterwards.  Results are written
to ``<output-dir>/scorecard.json`` (machine-readable, comparable across runs)
and ``<output-dir>/scorecard.md`` (human summary).  When ``--previous`` points
at an earlier ``scorecard.json``, regressions (pass -> fail, or recovery time
up by more than ``--regression-tolerance``) are flagged.

Exit codes
    0  every experiment passed and no regression was detected
    1  at least one experiment failed or regressed
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CHAOS_DIR = Path(__file__).resolve().parent

# experiment file -> {"inject_wait_s": seconds to wait after apply,
#                     "expect_degraded": /health circuit expected to degrade}
EXPERIMENTS = {
    "pod-kill-api.yaml": {"inject_wait_s": 15},
    "pod-kill-ingestion.yaml": {"inject_wait_s": 15},
    "network-partition-ingestion.yaml": {"inject_wait_s": 10},
    "network-partition-redis.yaml": {"inject_wait_s": 5, "expect_degraded": "feature_store_redis"},
}


def _load_verify():
    spec = importlib.util.spec_from_file_location(
        "verify_experiment", CHAOS_DIR / "verify_experiment.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kubectl(action: str, path: Path) -> None:
    args = ["kubectl", action, "-f", str(path)]
    if action == "delete":
        args.append("--ignore-not-found=true")
    subprocess.run(args, check=True)


def run_experiment(name: str, config: dict, health_url: str, timeout_s: int, verify) -> dict:
    path = CHAOS_DIR / name
    result = {
        "experiment": name,
        "passed": False,
        "degraded_s": None,
        "recovery_s": None,
        "error": None,
    }
    try:
        _kubectl("apply", path)
        time.sleep(config.get("inject_wait_s", 0))
        circuit = config.get("expect_degraded")
        if circuit:
            result["degraded_s"] = round(
                verify.assert_degraded(health_url, circuit, timeout_s=timeout_s), 1
            )
        result["recovery_s"] = round(
            verify.assert_recovery(health_url, timeout_s=timeout_s, circuit=circuit), 1
        )
        result["passed"] = True
    except (AssertionError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            _kubectl("delete", path)
        except (OSError, subprocess.CalledProcessError) as exc:
            result["error"] = result["error"] or f"cleanup failed: {exc}"
            result["passed"] = False
    return result


def find_regressions(current: dict, previous: dict | None, tolerance: float) -> list[str]:
    if not previous:
        return []
    before = {r["experiment"]: r for r in previous.get("results", [])}
    regressions = []
    for r in current["results"]:
        prev = before.get(r["experiment"])
        if prev is None:
            continue
        if prev["passed"] and not r["passed"]:
            regressions.append(f"{r['experiment']}: passed previously, now failing")
        elif (
            prev["passed"]
            and r["passed"]
            and prev.get("recovery_s")
            and r.get("recovery_s") is not None
            and r["recovery_s"] > prev["recovery_s"] * (1 + tolerance)
        ):
            regressions.append(
                f"{r['experiment']}: recovery {prev['recovery_s']}s -> {r['recovery_s']}s"
            )
    return regressions


def render_markdown(scorecard: dict) -> str:
    lines = [
        f"## Resilience scorecard — {scorecard['generated_at']}",
        "",
        (
            f"**Score:** {scorecard['passed']}/{scorecard['total']} experiments passed "
            f"({scorecard['score_pct']}%)"
        ),
        "",
        "| Experiment | Result | Degraded after (s) | Recovery (s) | Error |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in scorecard["results"]:
        lines.append(
            f"| `{r['experiment']}` | {'✅ pass' if r['passed'] else '❌ fail'} "
            f"| {r['degraded_s'] if r['degraded_s'] is not None else '—'} "
            f"| {r['recovery_s'] if r['recovery_s'] is not None else '—'} "
            f"| {r['error'] or ''} |"
        )
    lines.append("")
    if scorecard["regressions"]:
        lines.append("### ⚠️ Regressions vs previous run")
        lines.extend(f"- {r}" for r in scorecard["regressions"])
    else:
        lines.append("No regressions vs previous run.")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run all chaos-mesh experiments and publish a scorecard."
    )
    parser.add_argument(
        "--health-url", default=os.environ.get("HEALTH_URL", "http://localhost:8000/health")
    )
    parser.add_argument(
        "--timeout", type=int, default=int(os.environ.get("HEALTH_TIMEOUT_S", "120"))
    )
    parser.add_argument("--output-dir", default="scorecard")
    parser.add_argument(
        "--previous", help="Path to a previous scorecard.json for trend comparison."
    )
    parser.add_argument(
        "--regression-tolerance",
        type=float,
        default=0.5,
        help="Allowed fractional increase in recovery time before flagging (default 0.5).",
    )
    args = parser.parse_args(argv)

    verify = _load_verify()
    results = [
        run_experiment(name, config, args.health_url, args.timeout, verify)
        for name, config in EXPERIMENTS.items()
    ]
    passed = sum(r["passed"] for r in results)
    scorecard = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": os.environ.get("GITHUB_SHA"),
        "total": len(results),
        "passed": passed,
        "score_pct": round(100 * passed / len(results), 1) if results else 0.0,
        "results": results,
    }
    previous = None
    if args.previous and Path(args.previous).is_file():
        previous = json.loads(Path(args.previous).read_text())
    scorecard["regressions"] = find_regressions(scorecard, previous, args.regression_tolerance)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorecard.json").write_text(json.dumps(scorecard, indent=2) + "\n")
    markdown = render_markdown(scorecard)
    (out / "scorecard.md").write_text(markdown)
    print(markdown)
    return 0 if passed == len(results) and not scorecard["regressions"] else 1


if __name__ == "__main__":
    sys.exit(main())
