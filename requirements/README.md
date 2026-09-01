# `requirements/` — Python dependency lockfiles

This directory holds the **compiled, hash-pinned dependency lockfiles** for every
Python install surface in `ledgerlens-core`. Each surface is a pair of files:

- **`<surface>.in`** — the *source*. A short list of what the surface needs,
  usually just a reference to `pyproject.toml` extras (`-e ".[test]"`) plus
  `-r` includes of other `.in` files.
- **`<surface>.txt`** — the *generated output*. The full, hash-pinned transitive
  dependency tree produced from the `.in` file by
  [`pip-compile`](https://github.com/jazzband/pip-tools) (via
  [`compile.sh`](compile.sh)).

`pyproject.toml` remains the canonical place for version constraints. The `.in`
files only wire extras together; the `.txt` files are what CI and the container
build actually install.

> The broader "how Python dependencies are managed" workflow — editing
> `pyproject.toml`, license policy, committing lockfile changes with the
> manifest change — lives in
> [`../CONTRIBUTING.md`](../CONTRIBUTING.md#how-dependencies-are-managed). This
> file documents the directory layout itself.

---

## The surfaces

| `.in` file | Dependency group | What it is for | Install |
|------------|------------------|----------------|---------|
| `base.in`  | base runtime (`-e .`) | The runtime dependency closure declared in `pyproject.toml [project.dependencies]`. Everything the API/pipeline needs to run in production / the container. | `make install` |
| `test.in`  | test (`base` + `.[test]`) | Base runtime plus pytest, coverage, fixtures — the surface CI installs for the Python test matrix. | `make install-test` |
| `dev.in`   | dev tooling (`test` + `.[dev]`) | Everything a local contributor needs: the test surface plus lint tools and the `fuzz` / `ml` / `chain` / `graphql` / `causal` / `federated` extras. Not installed in CI or the container. | `make install-dev` |
| `docs.in`  | docs build (`.[docs]` + `-e .`) | The MkDocs site-build surface: `mkdocs`, theme, plugins, and the project itself (so `mkdocstrings` can resolve docstrings). Deliberately does **not** include the base runtime. | `make install-docs` |
| `fuzz.in`  | fuzzing (`base` + `.[fuzz]`) | Base runtime plus Atheris and fuzzing helpers, used by the nightly Python fuzz campaign. | `make install-fuzz` |
| `ml.in`    | ML (`base` + `.[ml]`) | Heavy ML surface: PyTorch, PyTorch-Geometric (GNN), MLflow, DP accounting. Install this when training the temporal / GNN models. | `make install-ml` |
| `chain.in` | chain-specific (`base` + `.[chain]`) | EVM cross-chain detection and Soroban submission-lease co-ordination: `web3`, EVM adapters, etc. Needed when `SOROBAN_SUBMISSION_LEASE_ENABLED=true` or when using the EVM bridge-loader. | `make install-chain` |

`compile.sh` processes the surfaces in dependency order
(`base test dev docs fuzz ml chain`) because later `.in` files `-r`-include
earlier ones.

---

## Regenerating the `.txt` lockfiles

Run this whenever you change a version constraint in `pyproject.toml` or edit
any `.in` file, then commit the updated `.txt` files **in the same commit** as
the change that caused them.

```bash
# Regenerate every lockfile (preferred — this is what `make lock` runs)
bash requirements/compile.sh

# Regenerate a single surface
bash requirements/compile.sh base

# Equivalent Make target
make lock
```

`compile.sh` requires `pip-tools`:

```bash
pip install pip-tools
```

It invokes `pip-compile` with `--generate-hashes --allow-unsafe --strip-extras
--no-emit-index-url --resolver=backtracking` for each surface.

### Verifying freshness

CI's `lock-check` job (in `.github/workflows/ci.yml`) runs
`pip-compile --dry-run` for each surface and **fails the PR if any `.txt` is
stale** relative to its `.in` source or `pyproject.toml`. Check locally with:

```bash
make lock-check
```

---

## Do not hand-edit the `.txt` files

Every `requirements/*.txt` file is **generated output**. Its header says so:

```
# AUTO-GENERATED – do not edit manually.
# Regenerate with: make lock   (or: bash requirements/compile.sh base)
```

Editing a `.txt` file directly — to bump a pin, drop a package, or add a hash —
will be undone the next time anyone runs `compile.sh`, and will fail
`make lock-check` in CI in the meantime. Change `pyproject.toml` (or the
relevant `.in` file) and recompile instead.

---

## See also

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md#how-dependencies-are-managed) — full dependency-management workflow, including the Rust / Go / TypeScript ecosystems.
- [`../docs/dependency_policy.md`](../docs/dependency_policy.md) — the license deny-list enforced on every dependency change.
- [`compile.sh`](compile.sh) — the regeneration script itself.
