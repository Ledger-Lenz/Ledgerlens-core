# Troubleshooting local development

This page expands the README quick start with fixes grounded in this
repository's setup scripts, configuration, and existing runbooks.

## Python dependencies

- Use Python 3.10+ as required by `pyproject.toml`, then run `pip install -r requirements/base.txt`.
- If a lock file is stale, edit the matching `requirements/*.in` input and run `bash requirements/compile.sh`; generated `.txt` files are not hand-edited.
- Install `requirements/test.txt` before running pytest; it supplies pytest-asyncio, Hypothesis, and fakeredis used by tests.

## Rust, Go, and TypeScript

- Contract checks use the pinned Soroban SDK in each `Cargo.toml`; run `cargo test --workspace` from the relevant workspace.
- Go components use their local `go.mod`; run `go test ./...` from that component directory when packages are missing.
- TypeScript packages are independent workspaces. Run `npm ci` in the package directory and use its declared scripts, not a global CLI.

## Database and Redis

- SQLite defaults to `./ledgerlens.db`; set `LEDGERLENS_DB_PATH` to a writable path and run `python cli.py db-migrate` for a fresh database.
- A degraded `db` health result means SQLite could not execute `SELECT 1`; check path permissions and migration state.
- Redis is optional locally. Set `REDIS_URL=redis://localhost:6379/0` for shared feature-store or rate-limit tests; an unreachable Redis uses the documented in-process fallback.

## First-run test failures

- Run `pytest` from the repository root after installing the test requirements.
- Scoring tests need model artifacts; generate them with `python cli.py train` as described in the README.
- `e2e`, `slow`, `chaos`, and `cross_repo_e2e` tests are excluded by default and require their external services when run explicitly.
