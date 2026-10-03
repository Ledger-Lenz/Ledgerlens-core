"""Deterministic, labeled wash-trading benchmark dataset.

Each pattern generator returns trade rows tagged with the pattern name; every
account is labeled 1 (wash) or 0 (clean). The dataset is versioned: bump
``DATASET_VERSION`` whenever generation changes, regenerate with
``python -m benchmarks.detection.dataset``, and re-baseline.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

DATASET_VERSION = "v1"
DATA_DIR = Path(__file__).parent / "datasets"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(src, dst, amount, ts, pattern):
    return {
        "base_account": src,
        "counter_account": dst,
        "base_amount": round(amount, 7),
        "ledger_close_time": ts.isoformat(),
        "pattern": pattern,
    }


def circular_trades(rng: random.Random, n_rings: int = 5):
    """A -> B -> C -> ... -> A cycles repeated with near-identical amounts."""
    rows, accounts = [], []
    for r in range(n_rings):
        ring = [f"CIRC{r}_{i}" for i in range(rng.randint(3, 6))]
        accounts += ring
        for rep in range(rng.randint(3, 8)):
            base = rng.uniform(500, 5000)
            t0 = EPOCH + timedelta(hours=rng.uniform(0, 24 * 25))
            for i, src in enumerate(ring):
                dst = ring[(i + 1) % len(ring)]
                rows.append(
                    _row(
                        src,
                        dst,
                        base * rng.uniform(0.99, 1.01),
                        t0 + timedelta(seconds=30 * i + rep),
                        "circular",
                    )
                )
    return rows, accounts


def self_matching(rng: random.Random, n_accounts: int = 5):
    """An account trading against its own offers."""
    rows, accounts = [], []
    for a in range(n_accounts):
        acct = f"SELF{a}"
        accounts.append(acct)
        for _ in range(rng.randint(5, 15)):
            ts = EPOCH + timedelta(hours=rng.uniform(0, 24 * 25))
            rows.append(_row(acct, acct, rng.uniform(100, 2000), ts, "self_matching"))
    return rows, accounts


def layering(rng: random.Random, n_groups: int = 4):
    """Two colluding accounts ping-pong volume through an intermediary layer."""
    rows, accounts = [], []
    for g in range(n_groups):
        a, b = f"LAYER{g}_A", f"LAYER{g}_B"
        layer = [f"LAYER{g}_L{i}" for i in range(rng.randint(2, 4))]
        accounts += [a, b, *layer]
        for _ in range(rng.randint(4, 10)):
            ts = EPOCH + timedelta(hours=rng.uniform(0, 24 * 25))
            amt = rng.uniform(1000, 8000)
            mid = rng.choice(layer)
            rows.append(_row(a, mid, amt, ts, "layering"))
            rows.append(_row(mid, b, amt * 0.998, ts + timedelta(minutes=2), "layering"))
            rows.append(_row(b, a, amt * 0.996, ts + timedelta(minutes=4), "layering"))
    return rows, accounts


def clean_traffic(rng: random.Random, n_retail: int = 80, n_makers: int = 5):
    """Retail accounts trading with market makers; a few organic reversals."""
    rows = []
    retail = [f"RETAIL{i}" for i in range(n_retail)]
    makers = [f"MAKER{i}" for i in range(n_makers)]
    for acct in retail:
        for _ in range(rng.randint(3, 20)):
            maker = rng.choice(makers)
            ts = EPOCH + timedelta(hours=rng.uniform(0, 24 * 30))
            amt = rng.lognormvariate(4, 1.5)
            src, dst = (maker, acct) if rng.random() < 0.05 else (acct, maker)
            rows.append(_row(src, dst, amt, ts, "clean"))
    return rows, retail + makers


PATTERNS = {
    "circular": circular_trades,
    "self_matching": self_matching,
    "layering": layering,
}


def build(seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(trades, labels)`` for the current dataset version."""
    rng = random.Random(seed)
    rows, labels = [], []
    for name, gen in PATTERNS.items():
        r, accts = gen(rng)
        rows += r
        labels += [{"account": a, "label": 1, "pattern": name} for a in accts]
    r, accts = clean_traffic(rng)
    rows += r
    labels += [{"account": a, "label": 0, "pattern": "clean"} for a in accts]
    trades = pd.DataFrame(rows).sort_values("ledger_close_time").reset_index(drop=True)
    return trades, pd.DataFrame(labels)


def write(version: str = DATASET_VERSION) -> Path:
    out = DATA_DIR / version
    out.mkdir(parents=True, exist_ok=True)
    trades, labels = build()
    trades.to_csv(out / "trades.csv", index=False)
    labels.to_csv(out / "labels.csv", index=False)
    return out


def load(version: str = DATASET_VERSION) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = DATA_DIR / version
    return pd.read_csv(out / "trades.csv"), pd.read_csv(out / "labels.csv")


if __name__ == "__main__":
    print(f"wrote {write()}")
