# Byzantine Resilience in Federated Learning

LedgerLens's federated aggregation server supports Krum and Multi-Krum —
Byzantine-fault-tolerant aggregation rules that protect gradient updates
against poisoning from malicious or compromised federation participants.

## Background

Plain FedAvg is broken by a single Byzantine client: one malicious
participant can submit an arbitrarily scaled gradient that shifts the global
model toward misclassifying wash-trading patterns.

Krum (Blanchard et al., 2017) selects the single client gradient **g_i** that
minimises the sum of squared Euclidean distances to its `n - f - 2` nearest
neighbours.  The rule is valid as long as `2f + 2 < n`.

Multi-Krum extends this by averaging the top-`m` scoring gradients instead of
a single one, offering a bias-variance tradeoff.

## Choosing `f`

`f` is the number of Byzantine clients you expect.  The default is
`floor(n / 3)`.  The hard constraint is `2f + 2 < n`; the server raises a
`ValueError` at startup if this is violated.

| `n` clients | Max safe `f` | Reasoning               |
|-------------|-------------|-------------------------|
| 5           | 1           | 2×1+2=4 < 5             |
| 7           | 2           | 2×2+2=6 < 7             |
| 10          | 3           | 2×3+2=8 < 10            |
| 50          | 15          | 2×15+2=32 < 50          |

If `n` is too small to achieve `f ≥ 1` (e.g., `n < 6` for `f=1`), the
constructor rejects the configuration with a clear error rather than silently
falling back to `f=0`.

## Multi-Krum Tradeoffs

| Mode           | `m` | Bias   | Variance | Notes                              |
|----------------|-----|--------|----------|------------------------------------|
| Standard Krum  | 1   | Lowest | Highest  | Single most-central gradient       |
| Multi-Krum     | >1  | Higher | Lower    | Average of top-m; approaches FedAvg as m→n |

Set `FL_MULTI_KRUM_M` > 1 when you have high gradient variance across honest
clients (e.g., heterogeneous data distributions).

## Aggregation Log Schema

Every round's decision is persisted to the `fl_aggregation_log` SQLite table:

```sql
CREATE TABLE fl_aggregation_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number     INTEGER NOT NULL,
    n_clients        INTEGER NOT NULL,
    f_tolerance      INTEGER NOT NULL,
    m_selected       INTEGER NOT NULL,
    selected_indices TEXT    NOT NULL,  -- JSON array of selected client indices
    excluded_indices TEXT    NOT NULL,  -- JSON array of excluded client indices
    krum_scores      TEXT    NOT NULL,  -- JSON array of float scores (lower = more central)
    recorded_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Query via the API:

```bash
curl -H "X-LedgerLens-Admin-Key: $LEDGERLENS_ADMIN_API_KEY" \
     "http://localhost:8000/admin/fl/aggregation?rounds=10"
```

## Persistent Byzantine-Actor Detection

`KrumStrategy` tracks per-client exclusion rates across rounds.  If a client
is excluded in more than 50% of consecutive rounds, a `WARNING` is logged:

```
Client <id> has been excluded in 60% of rounds — possible persistent Byzantine actor
```

Investigate that client's data pipeline or consider rotating it out of the
federation.

## Security Notes

- **Score logging only**: Krum scores (scalars) and client indices are logged.
  Gradient vectors are never persisted — they can be inverted to reconstruct
  private training data.
- **f validation**: enforced at `KrumStrategy` construction; a misconfigured
  `f` fails fast rather than silently providing weaker guarantees.
- **Mid-round dropout**: if the number of submitted gradients falls below
  `2f + 2 + 1` (e.g., clients drop out after the round starts), `krum_scores`
  raises a `ValueError`.  The calling code should fall back to FedAvg or abort
  the round depending on the operator's risk tolerance.

## References

- Blanchard, P. et al. (2017) *Machine Learning with Adversaries: Byzantine
  Tolerant Gradient Descent*. NeurIPS 2017.
