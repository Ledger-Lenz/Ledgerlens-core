#!/usr/bin/env python3
"""Render the supply-chain status dashboard (Grand 5 / #702).

Takes the "latest CI-passed commit" and "latest deployed commit/digest"
facts gathered by .github/workflows/deploy-status.yml and produces three
views of the same data: a static HTML page (for GitHub Pages), a machine-
readable status.json (for anything else that wants to consume this
programmatically), and a GitHub Actions job-summary Markdown fragment (so
the answer is visible even without Pages configured).

Deliberately has no third state between "match" and "mismatch" beyond
"data unavailable" — this script does not try to guess whether a mismatch
is *expected* (e.g. a CI-passed commit that simply hasn't been deployed
yet vs. a stuck/broken CD pipeline); that judgment belongs to whoever reads
the page, with the raw timestamps in front of them to make it.
"""
from __future__ import annotations

import argparse
import html
import json
import sys


def build_status(args: argparse.Namespace) -> dict:
    ci_found = args.ci_found == "true"
    deploy_found = args.deploy_found == "true"
    match = ci_found and deploy_found and args.ci_sha == args.deploy_sha

    return {
        "repo": args.repo,
        "generatedAt": _utc_now_iso(),
        "lastCiPassed": (
            {
                "commitSha": args.ci_sha,
                "runUrl": args.ci_run_url,
                "runAt": args.ci_run_at,
            }
            if ci_found
            else None
        ),
        "lastDeployed": (
            {
                "commitSha": args.deploy_sha,
                "imageDigest": args.deploy_digest,
                "deployedAt": args.deploy_at,
                "workflowRunId": args.deploy_run_id,
            }
            if deploy_found
            else None
        ),
        "match": match if (ci_found and deploy_found) else None,
    }


def _utc_now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_html(status: dict) -> str:
    ci = status["lastCiPassed"]
    dep = status["lastDeployed"]
    match = status["match"]

    if match is True:
        badge_class, badge_text = "ok", "IN SYNC"
    elif match is False:
        badge_class, badge_text = "warn", "OUT OF SYNC"
    else:
        badge_class, badge_text = "unknown", "UNKNOWN"

    def esc(v):
        return html.escape(str(v)) if v is not None else "&mdash;"

    ci_block = (
        f"""
        <dl>
          <dt>Commit</dt><dd><code>{esc(ci['commitSha'])}</code></dd>
          <dt>CI run</dt><dd><a href="{esc(ci['runUrl'])}">{esc(ci['runUrl'])}</a></dd>
          <dt>Passed at</dt><dd>{esc(ci['runAt'])}</dd>
        </dl>
        """
        if ci
        else "<p class='missing'>No successful CI run found for main.</p>"
    )

    dep_block = (
        f"""
        <dl>
          <dt>Commit</dt><dd><code>{esc(dep['commitSha'])}</code></dd>
          <dt>Image digest</dt><dd><code>{esc(dep['imageDigest'])}</code></dd>
          <dt>Deployed at</dt><dd>{esc(dep['deployedAt'])}</dd>
          <dt>CD run id</dt><dd>{esc(dep['workflowRunId'])}</dd>
        </dl>
        """
        if dep
        else "<p class='missing'>No deployment record found.</p>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{esc(status['repo'])} — Supply Chain Status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #0b0e14; --panel: #131720; --text: #e6e9ef; --muted: #8b93a7;
    --ok: #2fbf71; --warn: #e5a531; --unknown: #6b7280; --border: #232838;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2.5rem 1.5rem; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  main {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .25rem; }}
  .repo {{ color: var(--muted); margin: 0 0 2rem; font-size: .9rem; }}
  .badge {{
    display: inline-block; padding: .3rem .8rem; border-radius: 999px;
    font-weight: 600; font-size: .8rem; letter-spacing: .04em; margin-bottom: 1.5rem;
  }}
  .badge.ok {{ background: rgba(47,191,113,.15); color: var(--ok); border: 1px solid var(--ok); }}
  .badge.warn {{ background: rgba(229,165,49,.15); color: var(--warn); border: 1px solid var(--warn); }}
  .badge.unknown {{ background: rgba(107,114,128,.15); color: var(--unknown); border: 1px solid var(--unknown); }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  @media (max-width: 620px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  section {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem 1.4rem; }}
  section h2 {{ font-size: .85rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 1rem; }}
  dl {{ margin: 0; }}
  dt {{ color: var(--muted); font-size: .78rem; margin-top: .7rem; }}
  dt:first-child {{ margin-top: 0; }}
  dd {{ margin: .15rem 0 0; word-break: break-all; }}
  code {{ font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }}
  a {{ color: #7aa2f7; }}
  .missing {{ color: var(--muted); font-style: italic; }}
  footer {{ color: var(--muted); font-size: .78rem; margin-top: 1.5rem; }}
</style>
</head>
<body>
<main>
  <h1>Supply Chain Status</h1>
  <p class="repo">{esc(status['repo'])}</p>
  <span class="badge {badge_class}">{badge_text}</span>
  <div class="grid">
    <section>
      <h2>Last CI-passed commit</h2>
      {ci_block}
    </section>
    <section>
      <h2>Last deployed commit</h2>
      {dep_block}
    </section>
  </div>
  <footer>Generated {esc(status['generatedAt'])} by .github/workflows/deploy-status.yml</footer>
</main>
</body>
</html>
"""


def render_summary(status: dict) -> str:
    ci = status["lastCiPassed"]
    dep = status["lastDeployed"]
    match = status["match"]
    verdict = {True: "✅ IN SYNC", False: "⚠️ OUT OF SYNC", None: "❔ UNKNOWN"}[match]

    lines = [
        "## Supply Chain Status",
        "",
        f"**{verdict}**",
        "",
        "| | Commit | Detail |",
        "|---|---|---|",
        f"| Last CI-passed | `{ci['commitSha'] if ci else '—'}` | {ci['runAt'] if ci else 'no successful CI run found'} |",
        f"| Last deployed | `{dep['commitSha'] if dep else '—'}` | {dep['deployedAt'] if dep else 'no deployment record found'} |",
    ]
    if dep:
        lines.append(f"| Image digest | `{dep['imageDigest']}` | |")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ci-found", required=True)
    p.add_argument("--ci-sha", default="")
    p.add_argument("--ci-run-url", default="")
    p.add_argument("--ci-run-at", default="")
    p.add_argument("--deploy-found", required=True)
    p.add_argument("--deploy-sha", default="")
    p.add_argument("--deploy-digest", default="")
    p.add_argument("--deploy-at", default="")
    p.add_argument("--deploy-run-id", default="")
    p.add_argument("--repo", required=True)
    p.add_argument("--out-html", required=True)
    p.add_argument("--out-json", required=True)
    p.add_argument("--out-summary", required=True)
    args = p.parse_args()

    status = build_status(args)

    with open(args.out_html, "w") as f:
        f.write(render_html(status))
    with open(args.out_json, "w") as f:
        json.dump(status, f, indent=2)
        f.write("\n")
    with open(args.out_summary, "a") as f:
        f.write(render_summary(status))

    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
