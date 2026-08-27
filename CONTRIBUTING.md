# Contributing to LedgerLens Core

Thank you for your interest in contributing. This document covers everything you
need to get a working local environment, run the tests, and make dependency or
feature changes.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [First-time setup](#first-time-setup)
3. [Ecosystem layout](#ecosystem-layout)
4. [Development workflow](#development-workflow)
5. [How dependencies are managed](#how-dependencies-are-managed)
6. [Adding or updating a dependency](#adding-or-updating-a-dependency)
7. [Optional features and import guards](#optional-features-and-import-guards)
8. [License and vulnerability policy](#license-and-vulnerability-policy)
9. [Before opening a PR](#before-opening-a-pr)
10. [Cross-repo changes](#cross-repo-changes)

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

## Development workflow

```bash
python cli.py generate-data   # generate synthetic labelled dataset
python cli.py train           # train the ensemble on synthetic data
python cli.py serve --reload  # run the local API while iterating
pytest -q                     # run the full test suite
make lint                     # ruff linting
make lock-check               # verify all lockfiles are up to date
```

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

## Before opening a PR

1. `pytest -q` passes
2. `make lint` passes (`ruff check .`)
3. `make lock-check` passes (lockfiles are not stale)
4. New features include tests
5. Documentation (`README.md`, `docs/`) is updated for any user-facing change
6. If adding or bumping a dependency: `pyproject.toml` **and** the affected
   `requirements/*.txt` files are committed in the same PR

---

## Cross-repo changes

If a change affects a **shared contract** — `RiskScore` schema, `Trade`/`Asset`
schemas, environment variables in `.env.example`, or the Soroban contract
interface — call it out in the PR description so the corresponding change can be
made in `ledgerlens-api`, `ledgerlens-contracts`, and/or `ledgerlens-dashboard`.
See the "LedgerLens Organization" section of `README.md` for details.
