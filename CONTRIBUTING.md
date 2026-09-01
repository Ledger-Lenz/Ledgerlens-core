# Contributing to LedgerLens Core

Thank you for your interest in contributing. This document covers everything you
need to get a working local environment, run the tests, and make dependency or
feature changes.

---

## Table of Contents

1. [Picking your first issue](#picking-your-first-issue)
2. [Prerequisites](#prerequisites)
3. [First-time setup](#first-time-setup)
4. [Ecosystem layout](#ecosystem-layout)
5. [Choosing which package manager and lockfile to update](#choosing-which-package-manager-and-lockfile-to-update)
6. [Development workflow](#development-workflow)
7. [TypeScript SDK development](#typescript-sdk-development)
8. [Mutation testing](#mutation-testing)
9. [How dependencies are managed](#how-dependencies-are-managed)
10. [Managing Python dependencies](#managing-python-dependencies)
11. [Adding or updating a dependency](#adding-or-updating-a-dependency)
12. [Optional features and import guards](#optional-features-and-import-guards)
13. [Working with protobuf definitions](#working-with-protobuf-definitions)
14. [License and vulnerability policy](#license-and-vulnerability-policy)
15. [Proposing changes](#proposing-changes)
16. [Before opening a PR](#before-opening-a-pr)
17. [Definition of Done checklist](#definition-of-done-checklist)
18. [Cross-repo changes](#cross-repo-changes)
19. [Protobuf style conventions](#protobuf-style-conventions)


---

## Picking your first issue

New to the project? Start with an issue labeled `good first issue` or one
that has a clear, self-contained description. A well-scoped issue usually
includes:

- A **Description** explaining the problem or gap.
- **Acceptance Criteria** as a checklist — the concrete conditions your PR
  needs to satisfy.
- **File/Folder Hints** pointing at the files most likely involved.

If any of these are missing or unclear, ask on the issue before starting —
it's much cheaper to clarify scope up front than to rework a PR later.

This repository spans several ecosystems; here's roughly where to look
depending on what you're interested in:

- **Python core** (detection pipeline, API, ML models) — repo root, `api/`,
  `detection/`, `ingestion/`.
- **Rust** (Soroban SDK crate) — `crates/`.
- **Soroban smart contracts** — `contracts/`.
- **Go SDK** — `go/`.
- **TypeScript SDK** — `sdk/`.

See [Ecosystem layout](#ecosystem-layout) below for how each ecosystem's
dependencies are managed, and [First-time setup](#first-time-setup) for how
to get a working local environment.

---

## Prerequisites

| Tool | Minimum version | Install |
|------|-----------------|---------|
| Python | 3.10 | [python.org](https://python.org) / `pyenv install 3.12` |
| pip | 24.0 | `pip install --upgrade pip` |
| pip-tools | 7.4.1 | `pip install pip-tools==7.4.1` |
| Go | 1.22 | [go.dev](https://go.dev) |
| Rust (stable) | latest | `rustup update stable` |
| Node | 18+ | [nodejs.org](https://nodejs.org) |
| Docker (optional) | 24+ | for chaos / container tests |

---

## First-time setup

```bash
# 1. Clone the repo
git clone https://github.com/Ledger-Lenz/Ledgerlens-core.git
cd Ledgerlens-core

# 2. Create a virtual environment (recommended)
python -m venv .venv && source .venv/bin/activate

# 3. Install all development dependencies from the committed lockfile
make install-dev

# 4. Copy the environment template and fill in any secrets you need locally
cp .env.example .env

# 5. Run the test suite to verify everything works
pytest -q
```

The `make install-dev` command installs `requirements/dev.txt` (which includes
the base runtime, test, lint, fuzz, chain, ML, GraphQL, causal, and federated
extras) and then installs the project itself in editable mode (`-e .`).

### Optional heavy extras

Some extras pull in very large packages (PyTorch, torch-geometric) that you may
not need for most contributions:

```bash
# EVM cross-chain detection only
make install-chain

# ML training / GNN / MLflow only
make install-ml

# Minimal (runtime + tests only, fastest install)
make install-test
```

---

## Ecosystem layout

This repository contains four dependency ecosystems. Each has its own canonical
manifest, lockfile, and update procedure:

| Ecosystem | Manifest | Lockfile | Update command |
|-----------|----------|----------|----------------|
| **Python** | `pyproject.toml` | `requirements/*.txt` | `make lock` |
| **Rust** | `Cargo.toml` + workspace members | `Cargo.lock` | `cargo update` + commit |
| **Go** | `go/go.mod` | `go/go.sum` | `cd go && go get -u ./... && go mod tidy` |
| **TypeScript SDK** | `sdk/package.json` | `sdk/package-lock.json` | `cd sdk && npm update` + commit |

---

## Choosing which package manager and lockfile to update

> **Full policy details** — including allowed and blocked license families, vulnerability SLAs, and the exception-granting process — live in [`docs/dependency_policy.md`](docs/dependency_policy.md). Read that document before adding any new dependency. This section is a quick-start orientation.

This repository contains **four independent dependency ecosystems**. When you add or upgrade a dependency, touch only the files that belong to the ecosystem you are working in:

| If you changed… | Ecosystem | Manifest to edit | Lockfile to regenerate | Command |
|---|---|---|---|---|
| Python source (`api/`, `detection/`, `ingestion/`, …) | **Python** | `pyproject.toml` | `requirements/*.txt` | `make lock` |
| Rust crates (`crates/`, `contracts/`) | **Rust** | `Cargo.toml` | `Cargo.lock` | `cargo add <crate>` then `cargo update` |
| Go SDK (`go/`) | **Go** | `go/go.mod` | `go/go.sum` | `cd go && go get <module> && go mod tidy` |
| TypeScript SDK (`sdk/`) | **TypeScript** | `sdk/package.json` | `sdk/package-lock.json` | `cd sdk && npm install <pkg>` |

### Key rules

- **Python** — never edit `requirements/*.txt` by hand. They are compiled from `pyproject.toml` by `pip-compile --generate-hashes`. Run `make lock` and commit the resulting files.
- **Rust** — `Cargo.lock` is committed and must stay consistent. After `cargo add` or `cargo update`, commit `Cargo.lock` in the same PR as the `Cargo.toml` change.
- **Go** — commit both `go/go.mod` and `go/go.sum` together. Run `go mod tidy` after `go get` to prune unused indirect dependencies.
- **TypeScript** — use exact or tightly-bounded constraints in `sdk/package.json`. Commit `sdk/package-lock.json` after `npm install`. Run `npm ci` to verify the lockfile is consistent before pushing.
- **Do not cross-pollinate** — a Python feature PR must not touch `Cargo.lock`; a Rust PR must not regenerate `requirements/*.txt`. Keep ecosystem changes isolated.

For license compliance requirements that apply to all four ecosystems, see [`docs/dependency_policy.md`](docs/dependency_policy.md).

---

## Development workflow

```bash
python cli.py generate-data   # generate synthetic labelled dataset
python cli.py train           # train the ensemble on synthetic data
python cli.py serve --reload  # run the local API while iterating
pytest -q                     # run the full test suite
make lint                     # ruff linting
make lock-check               # verify all lockfiles are up to date
```

The commands above cover the Python engine. The TypeScript SDK in `sdk/` uses a
completely separate toolchain (npm / `tsc` / vitest) — see the next section.

---

## TypeScript SDK development

The `@ledgerlens/sdk` package in `sdk/` is a standalone TypeScript client for
the LedgerLens API. It does **not** share the Python toolchain — no Makefile
targets, no `requirements/`, no `pytest`. Everything is driven through npm from
inside the `sdk/` directory.

```bash
cd sdk
npm install          # install dependencies (writes sdk/package-lock.json)
npm test             # run the vitest suite once (sdk/tests/)
npm run test:watch   # run vitest in watch mode while iterating
npm run typecheck    # tsc --noEmit — type-check without emitting output
npm run build        # produce the dual CJS + ESM + types build under sdk/dist/
```

> **Note:** the type-check script is named `typecheck` (renamed from the
> misleadingly-named `lint` in
> [#776](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/776)). There is
> no real linter yet — `npm run typecheck` only checks types. ESLint + Prettier
> configuration is tracked in
> [issue #774](https://github.com/Ledger-Lenz/Ledgerlens-core/issues/774).

### Build targets

`npm run build` runs three separate `tsc` invocations, one per `tsconfig.*.json`
in `sdk/`. All three extend the base `sdk/tsconfig.json` (strict mode, ES2020
target, `src/` in, `tests/` excluded):

| Config | Script | Output | Purpose |
|--------|--------|--------|---------|
| `tsconfig.esm.json` | `npm run build:esm` | `sdk/dist/esm/` | ES module build (`module: ESNext`), referenced by `package.json`'s `module` / `exports.import` fields. |
| `tsconfig.cjs.json` | `npm run build:cjs` | `sdk/dist/cjs/` | CommonJS build (`module: CommonJS`, `moduleResolution: Node`), referenced by `main` / `exports.require`. |
| `tsconfig.types.json` | `npm run build:types` | `sdk/dist/types/` | Type declarations only (`emitDeclarationOnly`), referenced by `types`. |

The dual build is what lets the SDK be consumed from both `import` and
`require` in Node.js as well as from bundlers/browsers.

### Publishing

`sdk/package.json` is configured for publication to npm as `@ledgerlens/sdk`
(`publishConfig.access: public`), and a `prepublishOnly` hook runs
`npm run build && npm run test` before any publish.

**TBD — needs investigation:** there is currently no npm-publish GitHub Actions
workflow (the `.github/workflows/` dir covers CI, CD/Docker, docs, and
vulnerability scans only) and no documented release process. As with the other
SDKs, the actual `npm publish` (npm credentials, version bump, tag) is a
maintainer release action, not part of a feature PR.

### Managing SDK dependencies

See [How dependencies are managed → TypeScript SDK](#how-dependencies-are-managed)
below for the `package.json` / `package-lock.json` update procedure.

---

## Mutation testing

Line/branch coverage tells you a line *ran* during the test suite; it does not
tell you a test would *fail* if that line were wrong. Mutation testing closes
that gap: it makes small semantic edits to the source ("mutants" — flip a `<` to
`<=`, swap `and` for `or`, replace a return value with `None`) and re-runs the
tests against each one. A mutant that still passes the suite ("survived") is a
blind spot — the tests don't actually pin down that behaviour.

We run [`mutmut`](https://github.com/boxed/mutmut) (`mutmut>=3.0.0,<4.0`, in the
`test` / `dev` extras) against the three core detection modules. The configuration
lives in `pyproject.toml` under `[tool.mutmut]`:

```toml
[tool.mutmut]
paths = ["detection/benford_engine.py", "detection/graph_engine.py", "detection/model_inference.py"]
tests_dir = "tests"
```

Run it with:

```bash
make mutation-test
```

which is equivalent to:

```bash
mutmut run --paths-to-mutate detection/benford_engine.py,detection/graph_engine.py,detection/model_inference.py
mutmut results --all
```

**Runtime expectations.** Each surviving/tested mutant runs the full test suite
once, so this is *much* slower than a normal `pytest` run — plan for **20–40+
minutes** on a typical laptop for a cold run over all three modules (longer on
first run, faster on re-runs thanks to `mutmut`'s cache in `.mutmut-cache`). To
iterate on a single file while working on it:

```bash
mutmut run --paths-to-mutate detection/benford_engine.py
mutmut results          # summary
mutmut show <id>        # view a specific surviving mutant as a diff
```

**When to run it.** Mutation testing is **not part of CI** — no workflow in
`.github/workflows/` invokes `mutmut`, and the mutation-score badge in
`README.md` is updated manually. It is **not** expected before every PR. Run it
locally when:

- you change detection logic in `detection/benford_engine.py`,
  `detection/graph_engine.py`, or `detection/model_inference.py`, or the tests
  covering them;
- you want to verify that new tests for those modules are actually assertion-
  sensitive and not just coverage-padding.

Kill surviving mutants by adding or tightening assertions until the score is back
to **≥ 80%** across the three modules (`mutmut results --all`). See
[docs/testing_guide.md](docs/testing_guide.md) for how this fits alongside the
Hypothesis and Atheris suites.

---

## How dependencies are managed

### Python

Python dependencies are managed in two layers:

1. **`pyproject.toml`** — the *canonical manifest*. All version constraints
   live here, in `[project.dependencies]` (runtime) and
   `[project.optional-dependencies]` (extras). Edit only this file when you
   want to add, remove, or change a constraint.

2. **`requirements/*.txt`** — *generated lockfiles*, one per install surface.
   These are committed to the repository and are produced by
   `pip-compile --generate-hashes`. CI and the container build install
   exclusively from these files. **Do not edit them manually.**

   | Surface | Lockfile | Install command |
   |---------|----------|-----------------|
   | Runtime (container) | `requirements/base.txt` | `make install` |
   | CI tests | `requirements/test.txt` | `make install-test` |
   | Local dev | `requirements/dev.txt` | `make install-dev` |
   | MkDocs build | `requirements/docs.txt` | `make install-docs` |
   | Atheris fuzz | `requirements/fuzz.txt` | `make install-fuzz` |
   | ML extras | `requirements/ml.txt` | `make install-ml` |
   | EVM/chain extras | `requirements/chain.txt` | `make install-chain` |

CI verifies freshness with `pip-compile --check` in the `lock-check` job. A
stale lockfile fails the PR.

See [`requirements/README.md`](requirements/README.md) for a per-file breakdown
of the `.in` / `.txt` / `compile.sh` layout in that directory.

### Rust

Rust uses the standard Cargo workspace. `Cargo.lock` is committed and verified
via `cargo check` / `cargo test` in CI. Run `cargo update` then commit the
updated `Cargo.lock` to update.

### Go SDK

The `go/go.mod` + `go/go.sum` pair is committed and verified by `go test ./...
-race` in CI. Run `go get -u ./... && go mod tidy` from the `go/` directory,
then commit both files.

### TypeScript SDK

The `sdk/package.json` uses exact or tightly-bounded version constraints.
`sdk/package-lock.json` is committed and used by `npm ci` in CI. Run
`npm update && npm ci` from `sdk/` to update, then commit `package-lock.json`.

---

## Managing Python dependencies

Python packages are declared in **three layers**. Edit the highest layer that
applies to your change and let the tooling regenerate everything below it —
**never hand-edit a compiled `.txt` lockfile**, as any manual change is wiped the
next time the lockfiles are regenerated.

| Layer | Files | Role | Edit it when… |
|-------|-------|------|---------------|
| Manifest | `pyproject.toml` | Canonical version constraints: `[project.dependencies]` (runtime) and `[project.optional-dependencies]` (`test`, `docs`, `fuzz`, `ml`, `chain`, `graphql`, `causal`, `federated`, `dev`) | Adding, removing, pinning, or widening any package version |
| Source | `requirements/*.in` | **Source of truth** for the `pip-compile` inputs — one per install surface. Each is tiny: it installs the project (`-e .` / `-e ".[<extra>]"`) and layers lower surfaces with `-r` (e.g. `test.in` is `-r base.in` + `-e ".[test]"`) | Adding a whole new install surface, changing how surfaces layer, or adding a raw requirement that cannot be expressed as a project dependency. **Do not pin versions here** — that is `pyproject.toml`'s job (see the header comment in `base.in`) |
| Lockfile | `requirements/*.txt` | **Compiled output** — fully resolved, hash-pinned dependency trees, one per `.in` file. Committed to the repo; CI and the container build install exclusively from these | Never by hand — regenerate instead (below) |

### Regenerating the lockfiles

After changing `pyproject.toml` or any `.in` file, regenerate every `.txt`:

```bash
make lock          # wrapper for: bash requirements/compile.sh
```

To regenerate a single surface, pass its name to the script:

```bash
bash requirements/compile.sh base      # regenerates only requirements/base.txt
```

`requirements/compile.sh` compiles the surfaces in dependency order
(`base test dev docs fuzz ml chain`), invoking for each one:

```
pip-compile --generate-hashes --allow-unsafe --strip-extras \
            --no-emit-index-url --resolver=backtracking \
            --output-file requirements/<surface>.txt requirements/<surface>.in
```

This requires `pip-tools` (`pip install pip-tools==7.4.1`). Run `make lock-check`
— the same check CI's `lock-check` job runs — to confirm no lockfile is stale;
a stale lockfile fails the PR. Commit `pyproject.toml` (or the changed `.in`
file) **and every changed `requirements/*.txt`** in the same commit.

### Which pair do I edit?

| Your change | Add the constraint to | Regenerate | Lockfiles that change |
|-------------|-----------------------|------------|-----------------------|
| New / upgraded **core runtime** package | `pyproject.toml` → `[project.dependencies]` | `make lock` | `base.txt` **and every other `*.txt`** (all surfaces include base) |
| **Dev-only** tool (linter, debugger, mutation tester) | `pyproject.toml` → `[project.optional-dependencies].dev` | `make lock` | `dev.txt` |
| **Test-only** package | `[project.optional-dependencies].test` | `make lock` | `test.txt`, `dev.txt` |
| **Docs-build** tool (MkDocs plugin, mkdocstrings handler) | `[project.optional-dependencies].docs` | `make lock` | `docs.txt` |
| **Fuzz / ML / chain** extra package | `[project.optional-dependencies].{fuzz,ml,chain}` | `make lock` | the matching `{fuzz,ml,chain}.txt` **and** `dev.txt` (the `dev` extra bundles them) |
| A brand-new install surface | a new `requirements/<name>.in`, a `pyproject.toml` extra, and a `Makefile` target | `make lock` | new `requirements/<name>.txt` |

For the full step-by-step version of each case, see
[Adding or updating a dependency](#adding-or-updating-a-dependency).

---

## Adding or updating a dependency

### Python — adding a new runtime dependency

1. Add the package with an upper-bounded version constraint to
   `[project.dependencies]` in `pyproject.toml`:
   ```toml
   "my-package>=1.2.0,<2.0"
   ```

2. Regenerate all lockfiles:
   ```bash
   make lock
   ```

3. Verify no disallowed licenses were introduced:
   ```bash
   make license
   ```

4. Commit both `pyproject.toml` and **all changed `requirements/*.txt` files**
   in the same commit:
   ```
   deps: add my-package 1.2.x
   ```

### Python — adding an optional dependency

1. Decide which extras group it belongs to (`test`, `docs`, `fuzz`, `ml`,
   `chain`, `graphql`, `causal`, `federated`). If none fits, discuss in the PR.

2. Add the constraint to the appropriate extra in `pyproject.toml`:
   ```toml
   [project.optional-dependencies]
   chain = [
       ...
       "my-optional-package>=2.0.0,<3.0",
   ]
   ```

3. Guard the import in the source file (see
   [Optional features and import guards](#optional-features-and-import-guards)).

4. Regenerate lockfiles:
   ```bash
   make lock
   ```

5. Commit `pyproject.toml` and the affected `requirements/*.txt` files.

### Python — upgrading an existing dependency

1. Widen or tighten the version constraint in `pyproject.toml`.
2. Run `make lock` to regenerate lockfiles.
3. Run `pytest -q` to verify nothing regressed.
4. Commit both files.

### Rust

```bash
# Update a specific crate
cargo update -p my-crate

# Update all crates (be careful — verify tests still pass)
cargo update

# Commit
git add Cargo.lock && git commit -m "deps(rust): update Cargo.lock"
```

### Go

```bash
cd go
go get my-module@v1.2.3
go mod tidy
cd ..
git add go/go.mod go/go.sum && git commit -m "deps(go): add my-module v1.2.3"
```

### TypeScript SDK

```bash
cd sdk
npm install my-package@^1.2.0
npm ci   # verify lock is consistent
cd ..
git add sdk/package.json sdk/package-lock.json && git commit -m "deps(ts): add my-package"
```

---

## Optional features and import guards

Packages in the `ml`, `chain`, `graphql`, `causal`, and `federated` extras are
**not** available in the base runtime. Any module that imports them must protect
the import so that:

- Importing the module on a base install does not crash with `ModuleNotFoundError`.
- Users get a clear, actionable install message when the feature is invoked.

**Preferred pattern** — `try/except` at the top of the file:

```python
try:
    from web3 import Web3
    _HAS_WEB3 = True
except ImportError:
    Web3 = None      # type: ignore[assignment,misc]
    _HAS_WEB3 = False

def ingest_evm_events(...):
    if not _HAS_WEB3:
        raise ImportError(
            "'web3' is required but is not installed.\n"
            "  Install the 'chain' extra:  pip install 'ledgerlens-core[chain]'"
        )
    ...
```

Availability sentinels and `require_*()` helpers for all optional extras are
centralised in `ledgerlens/_optional_imports.py`.

---

## Working with protobuf definitions

The files under `generated/` (`scoring_pb2.py`, `scoring_pb2.pyi`,
`scoring_pb2_grpc.py`) are compiled output from `proto/ledgerlens/v1/scoring.proto`.
**Never hand-edit files in `generated/`** — any change must be made to the
`.proto` source and then regenerated.

After editing `proto/ledgerlens/v1/scoring.proto`, regenerate with (from the repo
root):

```bash
python -m grpc_tools.protoc -I proto/ledgerlens/v1 \
    --python_out=generated --grpc_python_out=generated \
    proto/ledgerlens/v1/scoring.proto
```

`grpc_tools.protoc` generates `scoring_pb2_grpc.py` with a bare
`import scoring_pb2`, which fails when imported as part of the `generated`
package. After regenerating, restore the relative-import-with-fallback at the
top of `scoring_pb2_grpc.py`:

```python
try:
    from . import scoring_pb2 as scoring__pb2
except ImportError:
    import scoring_pb2 as scoring__pb2
```

Also note that the committed gencode version must not exceed the `protobuf`
runtime pinned in `requirements/base.txt` — a newer `protoc` than the pinned
runtime produces gencode that runtime refuses to load. See the module
docstring in `generated/__init__.py` for further detail.

---

## License and vulnerability policy

LedgerLens ships only packages with permissive licenses. The CI
`license-vuln-scan` workflow enforces this automatically on every push that
touches a dependency file, and nightly for new CVEs.

**Blocked licenses**: GPL, AGPL, LGPL, CC-BY-SA. Any package carrying one of
these licenses will fail the `python-licenses` CI job.

**Granting an exception**: If a dependency with a non-permissive license is
unavoidable, open a PR that:
1. Documents the business justification in `docs/dependency_policy.md`.
2. Adds the package to the allow-list in the CI license-check script.
3. Gets sign-off from a maintainer.

**Vulnerability response**: The `python-vuln`, `rust-audit`, `go-vuln`, and
`ts-audit` CI jobs run `osv-scanner`, `cargo audit`, `govulncheck`, and
`npm audit --audit-level=high` respectively. Any new **high** or **critical**
CVE fails the PR. For medium/low findings, open an issue and track remediation.

Generate a fresh local report at any time:

```bash
make license     # license inventory → reports/licenses-python.csv
make audit-py    # osv-scanner against requirements/base.txt
make audit-rust  # cargo audit
make audit-go    # govulncheck
make audit-ts    # npm audit
make audit       # all of the above
```

---

## Proposing changes

Before writing any code, open an issue so the approach can be discussed
and scoped with maintainers. This avoids wasted effort if the direction
turns out to conflict with the roadmap or an in-flight PR.

### Reporting a bug

Use the **[Bug report]** issue form:

```
https://github.com/Ledger-Lenz/Ledgerlens-core/issues/new?template=bug_report.yml
```

The form prompts for a summary, reproduction steps, expected and actual
behaviour, the affected component (dropdown covering every subsystem from
`ingestion` through `contracts` and all SDKs), and your environment
details. Blank issues are disabled — GitHub will direct you to the
template picker automatically when you click "New issue".

### Proposing a new feature or enhancement

Use the **[Feature request]** issue form:

```
https://github.com/Ledger-Lenz/Ledgerlens-core/issues/new?template=feature_request.yml
```

The form asks for:

| Field | Why it matters |
|-------|----------------|
| **Problem statement** | Grounds the feature in a concrete pain point rather than an abstract wish. |
| **Proposed solution** | Gives maintainers enough detail to assess feasibility and spot conflicts with existing design. |
| **Alternatives considered** | Shows you've thought through the trade-offs, which speeds up review. |
| **Affected component** | Multi-select dropdown so the right maintainer is looped in early. |
| **Cross-repo impact** | Flags whether `ledgerlens-api`, `ledgerlens-contracts`, or `ledgerlens-data` also need changes (see [Cross-repo changes](#cross-repo-changes)). |
| **PR willingness** | Helps maintainers prioritise and match contributors to open work. |

Check [ROADMAP.md](ROADMAP.md) before filing — your feature may already
be tracked there.

---

## Before opening a PR

Run through the [Definition of Done checklist](#definition-of-done-checklist) below before
opening a PR. It lists the exact commands for every part of the codebase, scoped to only the
language(s) you actually touched.

---

## Definition of Done checklist

Run only the checks that apply to the parts of the codebase you changed. Tick each item before
opening a PR.

### Python (`api/`, `detection/`, `ingestion/`, `audit/`, `backtesting/`, …)

- [ ] **Tests pass** — `pytest -q`
- [ ] **Lint passes** — `make lint` (runs `ruff check .`)
- [ ] **Type-check passes** — `make typecheck` (runs `mypy` if configured, otherwise skip)
- [ ] **Lockfiles are fresh** — `make lock-check` (runs `pip-compile --check`; fails if stale)
- [ ] **New public behaviour has tests** — unit or integration test added/updated
- [ ] **User-facing docs updated** — `README.md` and/or `docs/` updated if the change affects
  API surface, CLI flags, config keys, or deployment behaviour

### Rust (`crates/`, `contracts/`)

- [ ] **Tests pass** — `cargo test --workspace`
- [ ] **Lint passes** — `cargo clippy --workspace -- -D warnings`
- [ ] **Format is clean** — `cargo fmt --check`
- [ ] **Lockfile committed** — `Cargo.lock` committed in the same PR as `Cargo.toml` changes

### Go (`go/`)

- [ ] **Tests pass (with race detector)** — `cd go && go test ./... -race`
- [ ] **Vet passes** — `cd go && go vet ./...`
- [ ] **Module files committed** — `go/go.mod` and `go/go.sum` committed together

### TypeScript SDK (`sdk/`)

- [ ] **Tests pass** — `cd sdk && npm test`
- [ ] **Lint passes** — `cd sdk && npm run lint`
- [ ] **Lockfile consistent** — `cd sdk && npm ci` completes without error; commit `sdk/package-lock.json`

### All changes

- [ ] **No debug or temporary code** — no `print`, `console.log`, `TODO: remove`, or commented-out
  blocks left behind
- [ ] **No secrets** — no API keys, secret keys, or credentials in any committed file
- [ ] **Branch is up to date** — `git fetch origin && git rebase origin/main` completes cleanly
- [ ] **PR description includes** `Closes #<issue>` for every resolved issue

> **Shortcut**: if your change only touches Python source, you only need the Python block and
> the "All changes" block. A Rust-only change needs only the Rust + All blocks, and so on.

For a breakdown of every GitHub Actions workflow — what each one checks, what
triggers it, and roughly how long it takes — see
[`docs/ci_workflows.md`](docs/ci_workflows.md).

---

## Cross-repo changes

If a change affects a **shared contract** — `RiskScore` schema, `Trade`/`Asset`
schemas, environment variables in `.env.example`, or the Soroban contract
interface — call it out in the PR description so the corresponding change can be
made in `ledgerlens-api`, `ledgerlens-contracts`, and/or `ledgerlens-dashboard`.
See the "LedgerLens Organization" section of `README.md` for details.

---

## Protobuf style conventions

The `.proto` definitions under `proto/ledgerlens/v1/` back the internal gRPC
Scoring Service — see `docs/grpc_scoring.md` for the service's purpose and
schema reference. When adding or changing a `.proto` file, follow the
conventions already established there:

- **Never reuse or renumber an existing field number.** Field numbers are part
  of the wire format; changing one breaks compatibility for existing gRPC
  consumers. Add new fields with the next unused number instead.
- **Add new fields as `optional`** (as `score_lower`, `score_upper`, and
  `coverage_guarantee` are in `RiskScoreProto`) so older clients that don't
  know about the field continue to work.
- **Use `snake_case` for field names** and `PascalCase` for message and
  service names, matching standard protobuf style.
- **Suffix wire-format messages with `Proto`** (e.g. `RiskScoreProto`) when a
  same-named domain model already exists elsewhere in the codebase, to avoid
  ambiguity between the two.
- **Document units and formats inline** with a trailing comment on the field
  (e.g. `uint32 score = 3; // 0-100`, `string timestamp = 7; // RFC3339`).
- Since this project is pre-1.0, breaking changes are still permitted but must
  be called out explicitly in the PR description per the
  [Cross-repo changes](#cross-repo-changes) policy above, since `RiskScore` is
  a shared contract.
