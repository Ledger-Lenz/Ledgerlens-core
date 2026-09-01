# Security Policy

LedgerLens handles on-chain smart contract interactions, API credentials, and
cryptographic proofs. If you believe you've found a security vulnerability,
please report it privately rather than filing a public GitHub issue.

## Reporting a Vulnerability

Please report suspected vulnerabilities using
[GitHub's private vulnerability reporting](https://github.com/Ledger-Lenz/Ledgerlens-core/security/advisories/new)
feature for this repository (Security tab → "Report a vulnerability"). This
opens a private advisory visible only to maintainers until a fix is ready.

> **TBD — needs maintainer input:** a dedicated security contact email has
> not yet been established for this project. Until one is documented here,
> GitHub private vulnerability reporting is the preferred channel.

When reporting, please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a proof-of-concept if available.
- The affected component (e.g. API, Soroban contract, ingestion pipeline, SDK).

We'll acknowledge reports as promptly as possible and keep you updated as the
issue is triaged and fixed.

## Supported Versions

LedgerLens does not yet maintain parallel release branches — security fixes
are applied to the `main` branch and released from there. See
[CHANGELOG.md](CHANGELOG.md) for release history.

## Related Documentation

For a detailed breakdown of trust boundaries, attack surface, and existing
mitigations, see [docs/threat_model.md](docs/threat_model.md).
