# Production rollback runbook

Grand 5 / #702, Required scope C: "rollback must be a defined, tested
procedure." This document defines it. See the **Verification status**
section at the bottom for exactly what has and has not been exercised
against a real cluster, and by whom.

## Before you start: what you're rolling back

`ledgerlens` is deployed via `helm/ledgerlens`, either as a plain
`Deployment` (`canary.enabled=false`, the default) or an Argo Rollouts
`Rollout` (`canary.enabled=true`). Every release — regardless of which mode
— carries traceability annotations set by `.github/workflows/cd.yml`:

```
ledgerlens.io/commit-sha:       <the exact commit that was built and scanned>
ledgerlens.io/image-digest:     <the exact image digest that was pushed>
ledgerlens.io/deployed-at:      <UTC timestamp>
ledgerlens.io/workflow-run-id:  <the CD run that performed this deploy>
```

These are on both the Deployment/Rollout object itself and its pod template,
so `kubectl get pods -l app.kubernetes.io/name=ledgerlens -o
jsonpath='{.items[0].metadata.annotations}'` answers "what commit is
actually running" from the live pods, not just the release history.

## Which path to use

| Situation | Use |
|---|---|
| Rolling back to the immediately-previous Helm release, and its image is still in the registry (the common case) | **Path A — `helm rollback`** |
| Rolling back to a commit further back than the last Helm revision, or the previous image was pruned from the registry | **Path B — redeploy a specific commit via `workflow_dispatch`** |
| `canary.enabled=true` (Argo Rollouts) and you need traffic reverted *immediately*, without re-running canary analysis steps | **Path A, then `kubectl argo rollouts undo`** — see the canary note below |

### Path A — `helm rollback` (fast path, no rebuild)

1. **Identify the target revision.**
   ```bash
   helm history ledgerlens
   ```
   Cross-check the revision you intend to roll back to against its recorded
   commit: `helm get values ledgerlens --revision <REV> | grep commitSha`.

2. **Record current state before changing anything** (so you have a
   before/after diff and a documented "what we rolled back from"):
   ```bash
   kubectl get deploy,rollout -l app.kubernetes.io/name=ledgerlens \
     -o jsonpath='{range .items[*]}{.metadata.annotations.ledgerlens\.io/commit-sha}{"\n"}{end}'
   ```

3. **Roll back.**
   ```bash
   helm rollback ledgerlens <REV> --wait --timeout 10m
   ```
   `cd.yml` deploys with `--history-max 20`, so the last 20 revisions are
   available to roll back to without needing to rebuild anything — this is
   what makes Path A fast.

4. **Verify.** Confirm the live annotation matches the target revision's
   commit, and that pods are Ready:
   ```bash
   kubectl get pods -l app.kubernetes.io/name=ledgerlens \
     -o jsonpath='{.items[0].metadata.annotations.ledgerlens\.io/commit-sha}'
   kubectl rollout status deployment/ledgerlens-api   # plain Deployment mode
   ```

**Canary-mode note:** if `canary.enabled=true`, `helm rollback` changes the
`Rollout` spec's image back to the old one, which by default makes Argo
Rollouts *re-run the canary steps forward* (20% → pause → 50% → pause →
100%) against the old image — correct behavior for a routine rollback, but
too slow if you're rolling back because production is actively broken. For
an urgent rollback, follow the `helm rollback` with:
```bash
kubectl argo rollouts undo ledgerlens-api
```
which is Argo Rollouts' own immediate-rollback primitive (analogous to
`kubectl rollout undo` for a plain Deployment) — it skips canary
progression and returns 100% of traffic to the previous ReplicaSet
directly.

### Path B — redeploy a specific commit (rebuild path)

Use this when the commit you need is not one of the last 20 Helm revisions,
or the image for it is no longer in the registry. `cd.yml` supports this
directly via its `workflow_dispatch` input, which **deliberately bypasses
the CI gate** (see the comment on that input in `cd.yml`) since you are
explicitly asking to deploy a specific already-known commit, not the
current `main` tip:

```bash
gh workflow run cd.yml --repo <org>/Ledgerlens-core -f sha=<commit-sha>
```

This re-runs the full build → scan → push → deploy pipeline against
`<commit-sha>`, including the Trivy gate — so a rollback target still has
to pass the same vulnerability gate a forward deploy would. If the commit
you're rolling back to genuinely can't pass current scan policy (e.g. a new
CVE was published against a dependency that was fine when that commit was
first deployed), that is a real signal to evaluate before forcing an old
build back into production, not something to bypass silently.

## Rollback rehearsal checklist (for a real cluster)

Run this end-to-end on a staging cluster (or, cautiously, production during
a maintenance window) at least once, and record real numbers here — a
runbook nobody has executed is a hypothesis, not a procedure:

- [ ] `helm rollback` timing, plain `Deployment` mode: target `< 2 min` to
      `Ready`.
- [ ] `helm rollback` + `kubectl argo rollouts undo`, canary mode: target
      `< 3 min` to 100% traffic on the old ReplicaSet.
- [ ] Path B (`workflow_dispatch` redeploy of a specific older commit):
      target `< 15 min` (bounded by the scan + Helm `--wait` steps in
      `cd.yml`), confirmed the Trivy gate still runs for the rollback
      target.
- [ ] Post-rollback annotation check: `ledgerlens.io/commit-sha` on the
      live pods matches the intended rollback target, not the commit being
      rolled back from.
- [ ] Confirm `helm history ledgerlens` still shows the rolled-back-from
      revision (rollback appends a new revision; it doesn't delete
      history).

## Verification status (read this before trusting the timings above)

This runbook was authored and the commands in it verified for syntactic
and semantic correctness against this repo's actual chart (`helm lint` /
`helm template` against `helm/ledgerlens`, confirming the annotations these
steps depend on actually render — see the PR description for that
evidence) and against `cd.yml`'s actual `workflow_dispatch` contract.

**It has not been executed against a live Kubernetes cluster** — this
sandbox has no cluster access, and exercising a real rollback requires the
target org's actual staging/production infrastructure and credentials,
which this PR does not have. The checklist above is scaffolding for that
rehearsal, not a substitute for it. The target timings are estimates based
on `--wait --timeout 10m` in `cd.yml` and typical `helm rollback` latency
for a small Deployment, not measurements.

**What was rehearsed for real**, live on GitHub Actions against the
`Ndifreke000/Ledgerlens-core` fork (no production credentials involved):
the CI-gate mechanism in `cd.yml` end to end (CI passing correctly lets CD
proceed, pinned to that exact `head_sha`); two rapid successive pushes to
`main`, where the first commit's CI run was itself superseded/cancelled by
the second (via `ci.yml`'s own concurrency group) and CD correctly refused
to build or deploy that superseded commit while correctly proceeding for
the second — with no interleaving or wrong-commit deploy; and the
dependency-vulnerability gate in `license-vuln-scan.yml` (a deliberately
pinned `PyYAML==5.1`, a known-CVE version, correctly blocked the OSV job).
The container-image Trivy gate in `cd.yml` was verified at the script/logic
level (build-then-scan-then-push ordering, `.trivyignore.yaml` waiver
parsing) but not via a live image scan, since that requires Docker Hub
push credentials this PR does not have — see the PR description for exact
run links, output, and this specific gap. A live rollback rehearsal
against a real cluster is the one verification item in the issue that
requires the maintainer's own infrastructure to complete; this document is
written so that completing it is a matter of following the checklist
above, not designing a procedure from scratch.
