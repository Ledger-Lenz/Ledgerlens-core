"""Causal inference for distinguishing wash trading from legitimate market making.

The ensemble in `detection.model_training` / `detection.model_inference` is
purely correlational: a legitimate market maker and a wash trader share the same
surface statistics (high frequency, tight spreads, appearing on both sides), so
the model flags both. This module adds a *causal* signal that separates them.

Price Discovery Contribution (PDC) is the Average Treatment Effect (ATE) of a
wallet's trades on the subsequent mid-price, estimated with a
Difference-in-Differences design over fixed time windows::

    PDC(w, p) = E[Δprice | w traded in window t] - E[Δprice | w did NOT trade]

A market maker improves price efficiency, so its presence *causes* mid-price
movement: PDC > 0. A wash trader self-deals without moving price, or the price
mean-reverts immediately: PDC ≈ 0 or negative.

Confounders (time-of-day, market volatility, pair liquidity depth) are
controlled with a doubly-robust inverse-propensity-weighted (DR-IPW) estimator
(Bang & Robins, 2005): an outcome-regression term gives consistency when the
propensity model is misspecified, and vice versa.
"""

import itertools
import logging
from functools import lru_cache
from typing import List

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger("ledgerlens.causal_engine")

# Confounders controlled for in the treatment-effect estimate.
_CONFOUNDERS = ["hour", "volatility", "volume"]
# Minimum windows (with both treated and control present) needed for a stable estimate.
_MIN_WINDOWS = 6
# Propensities are clipped to keep IPW weights finite under near-separation.
_PROPENSITY_CLIP = (0.05, 0.95)


def propensity_score(features: pd.DataFrame) -> np.ndarray:
    """Logistic propensity ``P(wallet trades | confounders)`` for IPW weighting.

    `features` must contain a ``treated`` column (0/1 treatment label) alongside
    the confounder columns. Confounders are standardised before fitting. When
    only one treatment class is present (no overlap), the empirical treatment
    rate is returned for every row instead of fitting a degenerate model.
    """
    df = features.copy()
    if "treated" not in df.columns:
        raise ValueError("propensity_score requires a 'treated' column in `features`")

    y = df.pop("treated").astype(int).to_numpy()
    X = df.to_numpy(dtype=float)
    n = len(y)
    if n == 0:
        return np.empty(0, dtype=float)

    if len(np.unique(y)) < 2:
        return np.full(n, float(y.mean()))

    X_scaled = StandardScaler().fit_transform(X)
    model = LogisticRegression(max_iter=1000)
    model.fit(X_scaled, y)
    proba = model.predict_proba(X_scaled)[:, 1]
    return np.clip(proba, *_PROPENSITY_CLIP)


def _doubly_robust_ate(panel: pd.DataFrame) -> float:
    """DR-IPW estimate of the ATE of treatment on outcome, controlling for confounders."""
    X = panel[_CONFOUNDERS].to_numpy(dtype=float)
    treated = panel["treated"].astype(int).to_numpy()
    outcome = panel["outcome"].astype(float).to_numpy()

    prop_input = panel[_CONFOUNDERS].copy()
    prop_input["treated"] = treated
    propensity = np.clip(propensity_score(prop_input), *_PROPENSITY_CLIP)

    # Outcome regression with treatment as an explicit covariate.
    design = np.column_stack([X, treated])
    reg = LinearRegression().fit(design, outcome)
    mu1 = reg.predict(np.column_stack([X, np.ones(len(treated))]))
    mu0 = reg.predict(np.column_stack([X, np.zeros(len(treated))]))

    dr_treated = treated * (outcome - mu1) / propensity + mu1
    dr_control = (1 - treated) * (outcome - mu0) / (1 - propensity) + mu0
    return float(np.mean(dr_treated) - np.mean(dr_control))


def _normalise_prices(prices: pd.DataFrame) -> pd.DataFrame | None:
    """Return prices as a sorted frame with `timestamp` and `mid_price` columns."""
    if prices is None or prices.empty:
        return None
    df = prices.copy()
    price_col = "mid_price" if "mid_price" in df.columns else "price" if "price" in df.columns else None
    if price_col is None or "timestamp" not in df.columns:
        return None
    df = df[["timestamp", price_col]].rename(columns={price_col: "mid_price"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["mid_price"] = pd.to_numeric(df["mid_price"], errors="coerce")
    df = df.dropna().sort_values("timestamp")
    return df if not df.empty else None


def _build_panel(
    trades: pd.DataFrame,
    prices: pd.DataFrame,
    wallet: str,
    pair: str | None,
    window_minutes: int,
) -> pd.DataFrame | None:
    """Build the windowed treatment/outcome/confounder panel, or `None` if infeasible."""
    price_df = _normalise_prices(prices)
    if price_df is None or trades is None or trades.empty:
        return None

    freq = f"{window_minutes}min"

    # Per-window mid price (last observation) and the *subsequent* price move.
    price_df = price_df.set_index("timestamp")
    window_mid = price_df["mid_price"].resample(freq).last().dropna()
    if len(window_mid) < _MIN_WINDOWS + 1:
        return None
    outcome = window_mid.shift(-1) - window_mid  # Δprice over the following window
    volatility = window_mid.rolling(3, min_periods=1).std().fillna(0.0)

    trades_df = trades.copy()
    trades_df["ledger_close_time"] = pd.to_datetime(trades_df["ledger_close_time"], utc=True)
    if pair is not None and "asset_pair" in trades_df.columns:
        trades_df = trades_df[trades_df["asset_pair"] == pair]

    if "base_amount" in trades_df.columns:
        amounts = pd.to_numeric(trades_df["base_amount"], errors="coerce").fillna(0.0)
    else:
        amounts = pd.Series(1.0, index=trades_df.index)
    trades_df = trades_df.assign(_amount=amounts.to_numpy())
    trades_df["_window"] = trades_df["ledger_close_time"].dt.floor(freq)

    counter = trades_df["counter_account"] if "counter_account" in trades_df.columns else pd.Series(
        [None] * len(trades_df), index=trades_df.index
    )
    wallet_mask = (trades_df["base_account"] == wallet) | (counter == wallet)

    volume = trades_df.groupby("_window")["_amount"].sum()
    treated_windows = set(trades_df.loc[wallet_mask, "_window"])

    panel = pd.DataFrame(
        {
            "outcome": outcome,
            "volatility": volatility,
        }
    ).dropna(subset=["outcome"])
    if panel.empty:
        return None

    panel["hour"] = panel.index.hour.astype(float)
    panel["volume"] = panel.index.map(lambda w: float(volume.get(w, 0.0)))
    panel["treated"] = panel.index.map(lambda w: 1 if w in treated_windows else 0)
    return panel


def estimate_pdc(
    trades: pd.DataFrame,
    prices: pd.DataFrame,
    wallet: str,
    pair: str,
    window_minutes: int = 5,
) -> float:
    """Estimate the price-discovery contribution of `wallet` on `pair`.

    Returns the ATE of the wallet's trades on the subsequent mid-price:
    positive => market-making (improves price discovery), near-zero or negative
    => wash-trading signal (self-dealing that does not move price, or mean
    reverts). Confounders are controlled with a doubly-robust IPW estimator.

    `prices` is a time series with `timestamp` and `mid_price` (or `price`)
    columns. `trades` is a `Trade`-shaped frame; when it carries an
    `asset_pair` column it is filtered to `pair`, otherwise it is assumed to be
    pre-filtered. Returns `0.0` when there is insufficient data or no treatment
    overlap to identify an effect.
    """
    panel = _build_panel(trades, prices, wallet, pair, window_minutes)
    if panel is None or len(panel) < _MIN_WINDOWS:
        return 0.0
    if panel["treated"].nunique() < 2:
        return 0.0

    try:
        return _doubly_robust_ate(panel)
    except Exception:
        # Fall back to the naive difference-in-means if the estimator is unstable.
        treated = panel[panel["treated"] == 1]["outcome"]
        control = panel[panel["treated"] == 0]["outcome"]
        if treated.empty or control.empty:
            return 0.0
        return float(treated.mean() - control.mean())


# ---------------------------------------------------------------------------
# PC-skeleton causal feature selection
# ---------------------------------------------------------------------------


def _partial_correlation(
    x: np.ndarray,
    y: np.ndarray,
    s_mat: np.ndarray,
) -> float:
    """Partial correlation of *x* and *y* given conditioning matrix *s_mat*.

    When *s_mat* has zero columns the ordinary Pearson correlation is returned.
    Residuals are computed via least-squares regression of each variable on the
    conditioning set.
    """
    if s_mat.shape[1] == 0:
        r = float(np.corrcoef(x, y)[0, 1])
    else:
        def _residual(v: np.ndarray) -> np.ndarray:
            coef, _, _, _ = np.linalg.lstsq(s_mat, v, rcond=None)
            return v - s_mat @ coef

        x_res = _residual(x)
        y_res = _residual(y)
        denom = np.std(x_res) * np.std(y_res)
        if denom < 1e-12:
            return 0.0
        r = float(np.cov(x_res, y_res)[0, 1] / (np.std(x_res) * np.std(y_res)))
    return float(np.clip(r, -1 + 1e-10, 1 - 1e-10))


def _fishers_z_test(
    x: np.ndarray,
    y: np.ndarray,
    s_mat: np.ndarray,
    alpha: float = 0.01,
) -> bool:
    """Return ``True`` if *x* ⊥ *y* | S (conditionally independent given S).

    Uses Fisher's Z transform of the partial correlation, with a two-tailed
    normal test. The statistic degenerates when ``n - |S| - 3 <= 0``; in that
    case the edge is retained (returns ``False``).

    Parameters
    ----------
    x, y:
        1-D feature vectors (``float64``).
    s_mat:
        N × |S| conditioning matrix; pass ``np.empty((n, 0))`` for the
        marginal test (|S| = 0).
    alpha:
        Significance level; edges are removed only when ``p > alpha``.
    """
    n = len(x)
    df = n - s_mat.shape[1] - 3
    if df <= 0:
        return False  # too few samples to test
    r = _partial_correlation(x, y, s_mat)
    z = 0.5 * np.log((1 + r) / (1 - r))
    se = 1.0 / max(np.sqrt(df), 1e-12)
    pval = 2.0 * (1.0 - norm.cdf(abs(z) / se))
    return bool(pval > alpha)


class CausalFeatureSelector:
    """Select features that are directly causally related to the wash-trading label.

    Implements the skeleton phase of the PC algorithm (Spirtes, Glymour &
    Scheines, 2000): edges between features and the target variable are tested
    for conditional independence at increasing conditioning set sizes.  A
    feature whose edge to the target survives all tests is *causally* connected
    to the label and retained; all others are pruned.

    Parameters
    ----------
    alpha:
        Significance level for Fisher's Z conditional independence tests.
        Lower values are more conservative (fewer edges removed).  Default: 0.01.
    max_conditioning_size:
        Maximum conditioning set size ``|S|`` in the PC skeleton phase.
        Values beyond 3 become computationally expensive for large feature sets.
        Default: 3.

    Attributes
    ----------
    selected_features_:
        List of feature names retained after fitting. Available after calling
        :meth:`fit`.
    n_features_in_:
        Total number of features passed to :meth:`fit`.
    separation_sets_:
        Mapping ``(feature_name, "label") → separation_set_indices``.
        Non-empty only for removed features.
    """

    def __init__(
        self,
        alpha: float = 0.01,
        max_conditioning_size: int = 3,
    ) -> None:
        self.alpha = alpha
        self.max_conditioning_size = max_conditioning_size
        self.selected_features_: List[str] = []
        self.n_features_in_: int = 0
        self.separation_sets_: dict = {}

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: List[str],
    ) -> List[str]:
        """Run the PC skeleton phase and return the selected feature names.

        Tests each feature for conditional independence from the label *y*,
        conditioning on growing subsets of the other features.  Features that
        become d-separated from *y* by some conditioning set are removed.

        Parameters
        ----------
        X:
            ``(n_samples, n_features)`` feature matrix (``float64``).
        y:
            ``(n_samples,)`` binary label vector (0/1).
        feature_names:
            List of feature names corresponding to columns of *X*.

        Returns
        -------
        List[str]
            Names of features retained in the causal subset, in their original
            order from *feature_names*.
        """
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        n, p = X.shape
        self.n_features_in_ = p

        if p == 0 or n < 10:
            self.selected_features_ = list(feature_names)
            return self.selected_features_

        # adj[i] = True means feature i is still adjacent to target y
        adj = [True] * p

        @lru_cache(maxsize=None)
        def _get_col(idx: int) -> bytes:
            return X[:, idx].tobytes()

        def _get_arr(idx: int) -> np.ndarray:
            return X[:, idx]

        for l in range(self.max_conditioning_size + 1):  # noqa: E741
            removed_this_round = False
            for i in range(p):
                if not adj[i]:
                    continue
                # Conditioning candidates: other features still adjacent to y.
                candidates = [j for j in range(p) if j != i and adj[j]]
                if len(candidates) < l:
                    continue
                for s_indices in itertools.combinations(candidates, l):
                    s_mat = X[:, list(s_indices)] if s_indices else np.empty((n, 0))
                    if _fishers_z_test(_get_arr(i), y, s_mat, self.alpha):
                        adj[i] = False
                        self.separation_sets_[(feature_names[i], "label")] = list(s_indices)
                        removed_this_round = True
                        logger.debug(
                            "CausalFeatureSelector: removed '%s' (sep set size=%d)",
                            feature_names[i], l,
                        )
                        break
            if not removed_this_round and l > 0:
                # No edges were removed in this pass; stop early.
                break

        self.selected_features_ = [
            name for name, is_adj in zip(feature_names, adj) if is_adj
        ]
        n_removed = p - len(self.selected_features_)
        logger.info(
            "CausalFeatureSelector: %d/%d features retained (%d removed)",
            len(self.selected_features_), p, n_removed,
        )
        return self.selected_features_
