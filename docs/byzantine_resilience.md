# Byzantine Resilience in Federated Learning

LedgerLens's federated aggregation server supports Krum and Multi-Krum —
Byzantine-fault-tolerant aggregation rules that protect gradient updates
against poisoning from malicious or compromised federation participants.

## Production wiring

`FederatedAggregationServer._aggregate_locked()` (`detection/federated/server.py`)
runs Multi-Krum peer-distance selection on every round via
`_select_krum_survivors()`, **default-on** (`settings.federated_use_krum = True`,
constructor override `use_krum=`). This supplements — does not replace — the
existing historical-cosine heuristic in `submit_update()`.

### Pipeline order

1. **Clip** (`submit_update`): each participant's delta is norm-clipped to
   `gradient_clip_threshold` before anything else touches it.
2. **Historical-cosine heuristic** (`submit_update`): flags/excludes updates
   whose delta direction diverges from the previous rounds' mean delta.
   Skipped on round 1 (no history yet) — this is exactly the gap Krum closes.
3. **Krum/Multi-Krum select** (`_aggregate_locked`, after quorum): computes
   pairwise distances over the (already clipped, already cosine-filtered)
   deltas of this round's valid updates and excludes the peer-distance
   outliers. Runs from round 1 onward.
4. **Weighted FedAvg** over the Krum survivors only, with the existing
   n_samples weighting and weight-share cap.
5. **Server-side DP noise**, added once to the final released aggregate —
   unchanged position from before Krum was wired in.

This ordering is a deliberate choice, not incidental:

- Clipping must precede Krum because an unbounded malicious delta would
  distort every pairwise distance in the round, defeating Krum's own
  guarantee before it runs.
- Krum must precede DP noise: noise added *before* Krum could push an honest
  update's measured distance closer to a malicious one (weakening the
  Byzantine-robustness guarantee), and Krum's data-dependent selection is
  only ever reflected downstream as a list of excluded participant ids (an
  already-audited quantity) rather than in any additional noised value — so
  running it before noising introduces no new privacy-budget surface, and
  the existing (ε, δ) accounting in `detection/federated/privacy_utils.py`
  and the RDP accountant in `server.py` are untouched.
- The cosine heuristic and Krum are kept **both active**: the cosine
  heuristic is cheap and catches sustained directional drift across rounds,
  while Krum is the structural fix for same-round collusion and the
  first-round gap. Neither subsumes the other's failure mode, so both stay.

### Quorum / `f` derivation (per round, not static)

`f` (Byzantine tolerance) and the `2f + 2 < n` safety margin are computed in
`_select_krum_survivors` from `n = len(valid_updates)` — the actual number of
non-excluded updates *in that round* — never from a static config value. This
keeps the guarantee valid even as participants join, drop out, or get
filtered by the cosine heuristic between rounds. Multi-Krum's `m` is set to
`n - f` (survive as many updates as the tolerance budget allows) so the
weighted FedAvg still benefits from the full non-outlier set rather than
collapsing to a single gradient.

**Fallback when the round is too small for any tolerance**: if `n < 3`, or no
`f >= 0` satisfies `2f + 2 < n` (i.e. `n <= 2`), Krum is skipped entirely for
that round with a `WARNING` log, and the round falls back to plain weighted
FedAvg with no per-round peer-distance defense (the cosine heuristic, if not
itself skipped, still applies). This is a documented, logged fallback rather
than a crash or a silent, invalid tolerance claim.

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
`floor(n / 3)`.  The hard constraint is `2f + 2 < n`.

In production (`FederatedAggregationServer`), `f` is derived automatically
each round from the live participant count (see "Quorum / `f` derivation"
above) — there is nothing to configure.

The standalone `KrumStrategy` class (`detection/federated/krum.py`, used
directly by tests and for offline/experimental aggregation outside the live
server) instead fixes `f` from a `min_clients` argument at construction, and
raises `ValueError` at construction if `2f + 2 < min_clients` doesn't hold.
Its default `min_clients` is **5** — the smallest `n` for which the default
`f = floor(min_clients / 3)` derivation is self-consistent (`floor(3/3)=1`
and `floor(4/3)=1` both give `2f+2=4`, which fails `< n` for `n=3` or `n=4`;
`n=5` is the first value where `2×1+2=4 < 5` holds). `KrumStrategy()` with no
arguments is guaranteed to construct successfully.

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

`KrumStrategy` (standalone use) tracks per-client exclusion rates across
rounds.  If a client is excluded in more than 50% of consecutive rounds, a
`WARNING` is logged:

```
Client <id> has been excluded in 60% of rounds — possible persistent Byzantine actor
```

Investigate that client's data pipeline or consider rotating it out of the
federation. In production, look for repeated "Krum round: excluded ..."
warnings from `FederatedAggregationServer._select_krum_survivors` for the
same participant across rounds.

## Security Notes

- **Score logging only**: Krum scores (scalars) and client indices are logged.
  Gradient vectors are never persisted — they can be inverted to reconstruct
  private training data.
- **f validation**: `KrumStrategy` (standalone use) validates `f` at
  construction; a misconfigured `f` fails fast rather than silently providing
  weaker guarantees. In production, `FederatedAggregationServer` instead
  derives `f` fresh each round from the live participant count, so there is
  no static value to misconfigure — see "Quorum / `f` derivation" above.
- **Mid-round dropout**: standalone `KrumAggregator.krum_scores` raises
  `ValueError` if the number of submitted gradients falls below `2f + 2 + 1`
  (e.g., clients drop out after the round starts). In production this is
  handled, not raised: `_select_krum_survivors` computes `f` from the live
  count and falls back to plain FedAvg with a `WARNING` (see "Fallback when
  the round is too small" above) instead of crashing or aborting the round.

## References

- Blanchard, P. et al. (2017) *Machine Learning with Adversaries: Byzantine
  Tolerant Gradient Descent*. NeurIPS 2017.
