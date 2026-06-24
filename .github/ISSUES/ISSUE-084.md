---
title: "Build CLI Subcommand for Bulk Historical Wallet Analysis with Progress Bar"
labels: ["difficulty: intermediate", "area: cli", "type: feature"]
assignees: []
---

## Summary
Operators want to score a list of wallets from a CSV file without writing scripts against the API. A `cli.py score bulk --input wallets.csv --output results.csv` command runs the full scoring pipeline locally (no API roundtrip) with a progress bar, parallel processing, and structured output.

## Objectives
- [ ] `cli.py score bulk --input FILE --output FILE [--concurrency N] [--min-score N]`
- [ ] Input CSV: one Stellar wallet address per row (with optional `label` column)
- [ ] Output CSV: wallet, score, confidence_lower, confidence_upper, top_features, scored_at
- [ ] Progress bar using `rich.progress` showing wallets/sec and ETA
- [ ] `--concurrency` default 4; max 16
- [ ] Skip malformed wallet addresses with a warning (do not abort)
- [ ] Write a `--dry-run` mode that validates the input file without scoring

## Definition of Done
- [ ] Scores 10k wallets in < 5 minutes on 4-core hardware with `--concurrency 4`
- [ ] Progress bar updates at least once per second
- [ ] Malformed addresses logged to stderr and excluded from output
- [ ] Tests cover empty input, all-malformed input, and concurrency limit
