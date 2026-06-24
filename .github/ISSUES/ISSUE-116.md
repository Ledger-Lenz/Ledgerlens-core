---
title: "Implement Semantic Versioning and Automated Release Pipeline with Changelog Generation"
labels: ["difficulty: intermediate", "area: devops", "type: feature"]
assignees: []
---

## Summary
LedgerLens has no formal release process: versions are not tagged, changelogs are not generated, and Docker images are not versioned beyond `latest`. Adopting semantic versioning with automated releases via GitHub Actions — triggered by conventional commits — provides a repeatable, auditable release process.

## Objectives
- [ ] Adopt Conventional Commits format across the codebase
- [ ] Configure `release-please` GitHub Action to auto-generate `CHANGELOG.md` and bump `pyproject.toml` version
- [ ] Tag releases as `v1.2.3` in Git; push Docker image as `ledgerlens/api:1.2.3` and `:latest`
- [ ] GitHub Release notes auto-generated from commit messages since last tag
- [ ] `cli.py --version` reports the current version from `pyproject.toml`

## Definition of Done
- [ ] Merging a conventional commit to main triggers the release-please PR
- [ ] Merging the release PR creates a Git tag and GitHub Release with changelog
- [ ] Docker image tagged with the release version is pushed to GHCR
- [ ] `ledgerlens --version` outputs the version matching `pyproject.toml`
