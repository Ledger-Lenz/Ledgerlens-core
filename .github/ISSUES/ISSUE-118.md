---
title: "Add Developer Documentation Site with MkDocs-Material and Auto-Generated API Docs"
labels: ["difficulty: intermediate", "area: documentation", "type: feature"]
assignees: []
---

## Summary
LedgerLens documentation is scattered across `docs/*.md` files with no consistent navigation, search, or visual structure. A MkDocs-Material site with auto-generated API reference, versioned documentation, and a CI deploy pipeline to GitHub Pages provides a professional documentation experience for contributors and integrators.

## Objectives
- [ ] Configure MkDocs-Material in `mkdocs.yml` with navigation mirroring the existing `docs/` structure
- [ ] Auto-generate Python API reference from docstrings using `mkdocstrings[python]`
- [ ] Deploy to GitHub Pages via GitHub Actions on every merge to main
- [ ] Add search, dark mode toggle, and copy-to-clipboard on code blocks
- [ ] Embed the OpenAPI spec (`docs/openapi.json`) as an interactive Swagger UI iframe

## Definition of Done
- [ ] `mkdocs serve` runs locally without errors
- [ ] GitHub Pages deployment live after merge to main
- [ ] All existing `docs/*.md` files appear in navigation
- [ ] Python API reference generated for `detection/`, `ingestion/`, and `api/` modules
