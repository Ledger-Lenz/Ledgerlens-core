# Contracts

This directory contains the on-chain contracts used by the risk-score registry.

## Storage-layout compatibility for upgrades

The risk-score registry is upgradeable. To prevent an upgrade from silently
corrupting existing on-chain state, every contract version must keep a
storage-layout snapshot and pass a compatibility check in CI.

### Snapshots

Each contract version has a committed storage-layout snapshot under
`contracts/storage-layouts/<contract>.json`. The snapshot is generated from the
compiled contract and records the ordered list of storage slots, their names,
types, and offsets for that version.

When a contract changes, regenerate the snapshot for the new version and commit
it alongside the code change. The snapshot is the source of truth that CI
compares against.

### CI check

CI runs a storage-layout compatibility check that compares the new version's
layout against the committed snapshot. The check fails when it detects an
incompatible change, such as:

- removing or renaming an existing storage field
- changing the type or size of an existing field
- reordering existing fields
- inserting a new field before existing fields

An incompatible change is only allowed when it is accompanied by an explicit
migration (see below). Without a migration, CI fails and the upgrade must be
revised.

### Supported upgrade patterns

Additive-only changes are the supported upgrade pattern:

- **Add new fields at the end** of the existing storage layout. New fields must
  not shift the slot or offset of any existing field.
- **Do not remove, rename, or retype** existing fields. If a field is no longer
  needed, leave it in place and stop using it rather than deleting it.
- **Do not reorder** existing fields.
- **Reserve gaps** when you expect future additions, so new fields can be placed
  in reserved slots without moving existing state.

Any change that is not additive-only requires an explicit migration that
transforms existing state into the new layout. Migrations must be reviewed and
must be referenced by the upgrade so the compatibility check can be satisfied
deliberately rather than by accident.

### Upgrade test

An upgrade test deploys the current version, writes representative state, then
upgrades to the new version and verifies that all pre-upgrade state is intact.
The test exercises the real upgrade path so that state integrity across the
upgrade is verified before release.
