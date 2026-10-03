# Releasing

Releases are automated by [release-please](https://github.com/googleapis/release-please)
through `.github/workflows/release.yml`. Configuration lives in
`release-please-config.json`, and current versions are tracked in
`.release-please-manifest.json`.

## Components

Each component is versioned and tagged on its own and keeps its own changelog:

| Component | Path | Tag | Changelog |
|---|---|---|---|
| Python core | `.` | `core-vX.Y.Z` | `CHANGELOG.md` |
| Go SDK | `go` | `go/vX.Y.Z` (required for Go submodule versioning) | `go/CHANGELOG.md` |
| TypeScript SDK | `sdk` | `ts-sdk-vX.Y.Z` | `sdk/CHANGELOG.md` |
| Python SDK | `packages/ledgerlens-sdk` | `python-sdk-vX.Y.Z` | `packages/ledgerlens-sdk/CHANGELOG.md` |
| FL client | `packages/ledgerlens-fl-client` | `fl-client-vX.Y.Z` | `packages/ledgerlens-fl-client/CHANGELOG.md` |
| FL server | `packages/ledgerlens-fl-server` | `fl-server-vX.Y.Z` | `packages/ledgerlens-fl-server/CHANGELOG.md` |
| Rust SDK | `crates/ledgerlens-sdk` | `rust-sdk-vX.Y.Z` | `crates/ledgerlens-sdk/CHANGELOG.md` |
| Oracle aggregator contract | `contracts/oracle_aggregator` | `oracle-aggregator-vX.Y.Z` | `contracts/oracle_aggregator/CHANGELOG.md` |
| ZK verifier contract | `contracts/zk_verifier` | `zk-verifier-vX.Y.Z` | `contracts/zk_verifier/CHANGELOG.md` |

A commit is only included in a component's release notes if it touches files
under that component's path. The Python core package (`.`) sets
`exclude-paths` for `go`, `sdk`, `crates`, `contracts` and `packages`, so
those commits do not appear in the root `CHANGELOG.md`. The existing
per-component changelogs (`go/CHANGELOG.md`, `crates/ledgerlens-sdk/CHANGELOG.md`,
and so on) are kept, and release-please adds new version entries above their
current content. Any that do not exist yet (for example `sdk/CHANGELOG.md`)
are created on that component's first release. Changelog sections
(`changelog-sections`) apply to every component.

## Maintainer workflow

1. Merge commits that follow Conventional Commits (`feat:`, `fix:`, `feat!:`)
   into `main`.
2. release-please opens **one release PR per component** that has changes.
   Each PR bumps that component's version and updates its changelog.
3. Review the PR's changelog. If it contains an entry from another component,
   that commit touched files in more than one component. Edit the PR if
   needed.
4. Merging the release PR creates a tag and a **draft** GitHub release for
   that component.
5. The `sbom` job generates the component's SBOM, attaches it, and then
   publishes the draft (see below). If the job fails, the release stays a
   draft. Fix the cause and re-run the job.

To check the scoping on a test release, run the workflow from a fork with
`workflow_dispatch`. Then confirm that each release PR lists only commits
under its own path.

## SBOMs

Every published release includes a CycloneDX JSON SBOM named
`<tag>.cdx.json` (with any `/` in the tag replaced by `-`) and a matching
`.sha256` checksum:

| Artifact | Generator |
|---|---|
| Python core wheel | `syft` over `requirements/base.txt` |
| Python packages (`packages/*`) | `cyclonedx-py` over a venv with the package installed |
| Go module | `syft` over `go/` (`go.mod`/`go.sum`) |
| npm package | `npm sbom --sbom-format cyclonedx` |
| Rust crate / contract WASM | `cargo cyclonedx` for the crate's manifest |

SBOM generation is a required step. A release is created as a draft and is
only published after a non-empty, valid CycloneDX SBOM has been uploaded, so
a published release cannot be missing its SBOM.

### Retrieving and verifying

```bash
TAG=rust-sdk-v0.2.0
FILE="${TAG//\//-}.cdx.json"   # "/" in tags (go/v0.2.0) becomes "-" in file names
gh release download "$TAG" --repo Ledger-Lenz/Ledgerlens-core --pattern "$FILE*"
sha256sum -c "$FILE.sha256"
jq '.bomFormat, .specVersion, (.components | length)' "$FILE"
```

You can pass the SBOM to any CycloneDX-aware tool, for example
`grype sbom:$TAG.cdx.json` or `osv-scanner --sbom=$TAG.cdx.json`.
