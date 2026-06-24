---
title: "Add Shell Auto-Completion Scripts for Bash, Zsh, and Fish"
labels: ["difficulty: beginner", "area: cli", "type: enhancement"]
assignees: []
---

## Summary
The LedgerLens CLI has no shell auto-completion, requiring users to memorise subcommand names and flags. Generating and installing completion scripts for Bash, Zsh, and Fish significantly improves CLI ergonomics for operators and developers.

## Objectives
- [ ] Add `cli.py completion --shell {bash,zsh,fish}` subcommand that prints the completion script
- [ ] Use Click's built-in `shell_complete` or `click-completion` library for script generation
- [ ] Document installation in `docs/cli_reference.md`: `eval "$(ledgerlens completion --shell zsh)"`
- [ ] Add completion for subcommand names, common flags (`--output`, `--concurrency`), and `--shell` enum values

## Definition of Done
- [ ] Tab completion works for all top-level subcommands and their flags in Bash and Zsh
- [ ] Install instructions validated on Ubuntu 22.04 and macOS 14
- [ ] CI smoke test: completion script generates without error for all three shells
