---
title: "Harden Soroban Circuit Breaker with Health Endpoint, Manual Reset, and Dead-Letter Queue"
labels: ["difficulty: advanced", "area: detection", "type: enhancement"]
assignees: []
---

## Summary

Productionise `detection/soroban_publisher.py` by adding: a `GET /admin/soroban/health` endpoint exposing circuit state, failure count, and last error; a `POST /admin/soroban/reset` endpoint for manual circuit reset; and a SQLite dead-letter queue (DLQ) for failed on-chain score submissions that can be inspected and replayed via `cli.py`. This transforms the existing circuit breaker from a silent safety mechanism into an observable, operable component of the LedgerLens production stack.

## Background & Context

`detection/soroban_publisher.py` already implements a circuit breaker that opens after `SOROBAN_CIRCUIT_BREAKER_THRESHOLD` consecutive failures and auto-resets after `SOROBAN_CIRCUIT_RESET_SECONDS`. However, when the circuit opens, the operator has no visibility into its state short of reading Python logs. There is also no persistence of which score submissions were dropped — those scores are silently lost, causing a gap between the on-chain registry and the local SQLite store.

For production reliability, three additions are needed:

1. **Observability**: `GET /admin/soroban/health` returns circuit state (`closed`, `open`, `half-open`), consecutive failure count, last error message, and time until auto-reset. Operators and monitoring systems (e.g., Prometheus scraper hitting `/admin/*`) can detect circuit-open conditions without parsing logs.

2. **Manual reset**: `POST /admin/soroban/reset` allows an operator to immediately close the circuit and clear the failure counter after fixing the underlying issue (e.g., re-funding the service account, redeploying the contract). Without this, the operator must wait for `SOROBAN_CIRCUIT_RESET_SECONDS` (default 300s) — unacceptable during a live incident.

3. **Dead-letter queue**: Instead of silently discarding failed submissions when the circuit is open, persist them to a `soroban_dead_letters` SQLite table. The `cli.py dlq-replay` command reads this table and retries submissions in order, applying the same retry logic as the live publisher. This prevents score gaps in the on-chain registry.

The existing `on_chain_submissions` audit table records every attempt; the DLQ is a separate, actionable table containing only items pending retry.

## Objectives

- [ ] Extend `SorobanPublisher` to track `circuit_state: Literal["closed", "open", "half-open"]`, `consecutive_failures: int`, `last_error: Optional[str]`, and `circuit_opened_at: Optional[datetime]`.
- [ ] Implement half-open state: after `SOROBAN_CIRCUIT_RESET_SECONDS`, the circuit transitions to `half-open`; the next submission is attempted as a probe; on success → `closed`; on failure → `open` again with reset timer.
- [ ] Implement `SorobanPublisher.health() -> SorobanHealthStatus` returning the current circuit state snapshot.
- [ ] Add `GET /admin/soroban/health` to `api/main.py`, admin-key gated, returning `SorobanHealthStatus`.
- [ ] Add `POST /admin/soroban/reset` to `api/main.py`, admin-key gated, that calls `SorobanPublisher.reset_circuit()` and returns the new health snapshot.
- [ ] Create `soroban_dead_letters` SQLite table via `cli.py db-migrate`.
- [ ] When the circuit is `open` and a submission is skipped, write the submission to `soroban_dead_letters` with status `pending`.
- [ ] Implement `cli.py dlq-replay` command that reads `pending` dead letters ordered by `created_at` and retries them via `SorobanPublisher.submit_batch()`.
- [ ] `dlq-replay` marks replayed items as `replayed` on success or `failed` on persistent failure; it never removes rows.
- [ ] `GET /admin/soroban/dead-letters` endpoint returns paginated DLQ contents, admin-key gated.
- [ ] All new code paths covered by tests with ≥90% branch coverage.

## Technical Requirements

### `SorobanHealthStatus` dataclass

```python
@dataclass
class SorobanHealthStatus:
    circuit_state: str          # "closed" | "open" | "half-open"
    consecutive_failures: int
    last_error: Optional[str]   # last error message, None if no failure
    circuit_opened_at: Optional[datetime]
    seconds_until_reset: Optional[float]   # None when circuit is closed
    dlq_pending_count: int      # rows in soroban_dead_letters with status='pending'
```

### Extended `SorobanPublisher`

```python
class SorobanPublisher:
    def health(self) -> SorobanHealthStatus:
        """Thread-safe snapshot of circuit breaker state."""
        ...

    def reset_circuit(self) -> SorobanHealthStatus:
        """
        Immediately close the circuit, clear consecutive_failures,
        and clear last_error. Returns new health snapshot.
        Logs at WARNING level: "Circuit manually reset by operator."
        """
        ...

    def _write_dead_letter(
        self,
        wallet: str,
        asset_pair: str,
        score: int,
        timestamp: int,
        error: str,
    ) -> None:
        """Write a failed submission to soroban_dead_letters with status='pending'."""
        ...
```

### Dead-letter SQLite schema

```sql
CREATE TABLE IF NOT EXISTS soroban_dead_letters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    asset_pair      TEXT NOT NULL,
    score           INTEGER NOT NULL,
    ledger_timestamp INTEGER NOT NULL,
    error_message   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'replayed', 'failed')),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    replayed_at     TIMESTAMP,
    replay_tx_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_dlq_status ON soroban_dead_letters(status);
CREATE INDEX IF NOT EXISTS idx_dlq_created ON soroban_dead_letters(created_at);
```

### `cli.py dlq-replay` command

```python
@app.command("dlq-replay")
def dlq_replay(
    limit: int = typer.Option(100, help="Max dead letters to replay per run"),
    dry_run: bool = typer.Option(False, help="Print DLQ contents without submitting"),
):
    """
    Replay pending Soroban dead-letter submissions.
    Processes oldest-first. Marks each as 'replayed' or 'failed'.
    """
    ...
```

### API endpoints (`api/main.py`)

```python
@router.get("/admin/soroban/health", response_model=SorobanHealthOut)
async def soroban_health(publisher: SorobanPublisher = Depends(get_publisher), ...): ...

@router.post("/admin/soroban/reset", response_model=SorobanHealthOut)
async def soroban_reset(publisher: SorobanPublisher = Depends(get_publisher), ...): ...

@router.get("/admin/soroban/dead-letters", response_model=PaginatedDeadLetters)
async def list_dead_letters(
    status: Optional[str] = Query(None),
    page: int = 1, page_size: int = 50,
    ...
): ...
```

### Half-open state machine

```
[closed] ---(threshold failures)---> [open]
[open]   ---(reset timeout)---------> [half-open]
[half-open] ---(probe success)-------> [closed]
[half-open] ---(probe failure)-------> [open]   (reset timer restarts)
[any]    ---(manual reset)-----------> [closed]
```

The `half-open` probe is transparent to callers: `submit_batch()` attempts a single real submission. Implement the state machine with a `threading.Lock` to ensure thread-safe transitions.

### Configuration additions to `.env.example`

```
# Dead-letter queue
SOROBAN_DLQ_MAX_ROWS=10000    # Hard cap; oldest rows pruned when exceeded
```

## Security Considerations

- Both `/admin/soroban/health` and `/admin/soroban/reset` must require `LEDGERLENS_ADMIN_API_KEY`. Return HTTP 503 (not 401) if the key is not configured, consistent with other admin endpoints.
- `POST /admin/soroban/reset` is a privileged mutation: it bypasses the circuit breaker's automatic protection. Log the event at `WARNING` level including the requesting IP address so resets are auditable.
- Dead-letter rows contain wallet addresses and scores but no secret keys. Treat the DLQ as moderately sensitive data — it reveals which wallets triggered high scores, which could be useful to an attacker. Ensure `GET /admin/soroban/dead-letters` is admin-key gated.
- `dlq-replay` reuses `SorobanPublisher`'s existing key management (keypair loaded from env, never logged). Replay should fail fast if `LEDGERLENS_SERVICE_SECRET_KEY` is not set rather than silently skipping submissions.
- Rate-limit `POST /admin/soroban/reset` to 10 calls per minute to prevent automated circuit-flapping attacks.

## Testing Requirements

- **Unit — half-open state machine**: simulate `threshold` failures → assert state is `open`; advance mock clock by `reset_seconds` → assert state is `half-open`; mock a successful probe → assert state is `closed`.
- **Unit — probe failure in half-open**: advance clock, mock probe failure → assert state returns to `open` with reset timer restarted.
- **Unit — manual reset**: open circuit, call `reset_circuit()` → assert state is `closed`, `consecutive_failures == 0`, `last_error is None`.
- **Unit — DLQ write on open circuit**: with circuit open, call `submit_batch()` → assert `soroban_dead_letters` row written with `status='pending'`.
- **Unit — `dlq-replay` success**: insert pending dead letter, mock successful submission → assert row status updated to `replayed`, `replay_tx_hash` populated.
- **Unit — `dlq-replay` failure**: insert pending dead letter, mock failed submission → assert row status updated to `failed`.
- **Integration — `GET /admin/soroban/health` 200**: assert response contains `circuit_state`, `dlq_pending_count`.
- **Integration — `GET /admin/soroban/health` 503**: missing admin key returns 503.
- **Integration — `POST /admin/soroban/reset`**: open circuit, POST reset, assert health shows `closed`.
- **Integration — `GET /admin/soroban/dead-letters`**: assert pagination works; assert `status` query filter works.

## Documentation Requirements

- Update `README.md` Soroban Integration section with the circuit breaker state machine diagram.
- Document `dlq-replay` in the CLI Reference table.
- Add `GET /admin/soroban/health`, `POST /admin/soroban/reset`, `GET /admin/soroban/dead-letters` to the Model Observability API table.
- New file `docs/soroban_operations.md` covering: circuit breaker states, health endpoint interpretation, DLQ replay procedure, and manual reset runbook.
- Update `.env.example` with `SOROBAN_DLQ_MAX_ROWS`.
- `CHANGELOG.md` entry under `## Unreleased`.

## Definition of Done

- [ ] Half-open state implemented in `SorobanPublisher` with thread-safe state machine.
- [ ] `SorobanPublisher.health()` and `reset_circuit()` implemented.
- [ ] `soroban_dead_letters` table created via `db-migrate`.
- [ ] Open-circuit submissions written to DLQ instead of silently discarded.
- [ ] `cli.py dlq-replay` command operational (including `--dry-run`).
- [ ] `GET /admin/soroban/health`, `POST /admin/soroban/reset`, `GET /admin/soroban/dead-letters` endpoints live and admin-key gated.
- [ ] All unit and integration tests pass; ≥90% branch coverage on state machine and DLQ code.
- [ ] `docs/soroban_operations.md` written and complete.
- [ ] `README.md`, `.env.example`, and `CHANGELOG.md` updated.

## For Contributors

**Ideal contributor profile**: You have experience building production-grade circuit breakers or resilience patterns in Python services, ideally for external API calls or blockchain RPC integrations. Familiarity with Soroban/Stellar transaction lifecycle is a significant advantage. You are comfortable with threading primitives (`threading.Lock`, `threading.Event`) for state machine safety, and with SQLite schema design for operational tables. Experience operating services with dead-letter queues (e.g., SQS DLQ, Kafka dead-letter topics) will translate directly.

To apply, please comment on this issue with:
1. **Specialty area**: your primary expertise (e.g., resilience engineering, blockchain integrations, Python backend).
2. **Relevant experience**: circuit breaker implementations, DLQ patterns, or Soroban/Stellar SDK usage you have shipped.
3. **Approach / thoughts**: your view on the half-open probe design — would you probe with a real submission or a read-only `get_score` call? What are the tradeoffs?
4. **Estimated time**: your realistic estimate to complete to the Definition of Done standard.
