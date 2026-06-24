---
title: "Implement Configuration Schema Validation with Fail-Fast Startup Checks"
labels: ["difficulty: intermediate", "area: infrastructure", "type: enhancement"]
assignees: []
---

## Summary
LedgerLens reads configuration from environment variables without validating types, ranges, or required fields at startup. Missing or malformed config (e.g., a non-integer `SCORE_ALERT_THRESHOLD`) causes cryptic runtime errors far from the startup path. A Pydantic v2 `Settings` model with validators that runs at import time provides immediate, actionable error messages for misconfiguration.

## Objectives
- [ ] Implement `config/settings.py` with a `pydantic_settings.BaseSettings` subclass covering all env vars
- [ ] Add validators for: port ranges, URL format, positive integers, enum values
- [ ] Mark required fields (no default); application startup aborts with a clear error listing all missing fields
- [ ] Replace all `os.environ.get(...)` calls throughout the codebase with `settings.FIELD_NAME`
- [ ] Add `cli.py config validate` that loads and prints the validated config (masking secrets)

## Definition of Done
- [ ] Starting with a missing required env var prints a human-readable error within 1 second and exits non-zero
- [ ] `cli.py config validate` lists all settings with current values (secrets masked)
- [ ] All `os.environ.get(...)` calls replaced
- [ ] Tests cover missing required field, out-of-range port, and invalid URL format
