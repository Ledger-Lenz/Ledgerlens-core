# docs/

This directory is the source for the LedgerLens Core documentation site, built with [MkDocs](https://www.mkdocs.org/) and the [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) theme, as configured in [`mkdocs.yml`](../mkdocs.yml). The built site is published at <https://ledger-lenz.github.io/Ledgerlens-core>.

For a project overview, installation instructions, and usage examples, see the root [README.md](../README.md) instead — this file only orients you within the `docs/` source tree.

## Building and serving locally

Install the docs dependencies and build or serve the site with the existing `make` targets:

```bash
make install-docs   # pip install -r requirements/docs.txt
make docs-serve      # mkdocs serve (live-reloading local preview)
make docs            # mkdocs build (static site output)
```

## Structure

The navigation in `mkdocs.yml` groups the Markdown files in this directory into:

- **Architecture** — subsystem design docs (ingestion, feature store, cross-chain detection, federated learning, adversarial robustness, uncertainty quantification, governance protocol, oracle network).
- **API Reference** — the REST API guide and `mkdocstrings`-generated module references under `api/`.
- **OpenAPI** — the OpenAPI specification page.

Most files in this directory are not yet wired into `mkdocs.yml`'s `nav`; they're still browsable directly on GitHub or via the site's search, and cover deeper topics such as Benford's Law analysis, database schema/migrations, compliance export, and CI/dependency policy.
