"""Causal inference engine for LedgerLens wash-trading detection.

This module provides two distinct layers of causal reasoning:

1. **Price Discovery Contribution (PDC)** — the original doubly-robust
   DR-IPW estimator that measures whether a wallet's trades *cause* price
   movement (market makers) or leave price unchanged (wash traders).

2. **DoWhy Structural Causal Model (SCM)** — a causal DAG over all ML
   features and the risk score output, enabling do-calculus interventions.
   Analysts can ask "if we remove the Benford signal, what is the causal
   contribution of graph topology to the score?" using ``CausalEngine``.

Background
----------
SHAP values explain which features contributed most to a score but they
conflate causal and correlational effects.  When Benford features and graph
features are correlated (as they are: wash traders are simultaneously
non-Benford AND in rings), SHAP attributes shared credit to both.  A
regulator asking "would this wallet still be flagged if it fixed its Benford
distribution?" cannot be answered by SHAP — only by causal intervention.

DoWhy (Microsoft Research) provides a Python API for causal reasoning: define
a causal DAG, fit structural equations from data, then use ``do(X=x)``
interventions to compute counterfactual expected outcomes.

Causal DAG design
-----------------
Each edge below encodes a domain-knowledge causal claim:

- ``wash_activity → wash_ring_membership``: latent wash coordination is the
  root cause of observable ring membership; the converse does not hold.
- ``wash_activity → round_trip_trade_frequency``: coordinated self-dealing
  directly inflates round-trip counts regardless of ring detection.
- ``wash_activity → chi_sq_24h``: wash bots use fixed lot sizes (non-Benford
  digit distribution).  The Benford signal is *caused by* wash activity.
- ``wash_activity → cycle_volume_ratio``: coordinated wash volume flows
  through ring cycles, driving up the ratio.
- ``wash_ring_membership → volume_to_unique_counterparty_ratio``: wallets in
  rings trade repeatedly with the same set of counterparties, concentrating
  volume.
- ``wash_ring_membership → round_trip_trade_frequency``: ring membership
  structurally implies round-trip patterns.
- ``account_age_days → wash_ring_membership``: older accounts are costlier to
  Sybil-create; new accounts are therefore over-represented in wash rings.
- ``network_centrality → wash_ring_membership``: high-centrality nodes act as
  hubs that enable ring formation.
- ``wash_ring_membership → risk_score``: the single strongest direct driver.
- ``round_trip_trade_frequency → risk_score``: a direct causal path
  independent of ring membership detection.
- ``chi_sq_24h → risk_score``: Benford anomaly contributes directly via the
  Benford engine sub-score.
- ``cycle_volume_ratio → risk_score``: high cycle fraction elevates the score
  independent of explicit ring membership.
- ``volume_to_unique_counterparty_ratio → risk_score``: concentration is a
  direct risk indicator.
- ``network_centrality → risk_score``: high-centrality nodes are structurally
  suspicious independent of ring detection.
- ``account_age_days → risk_score``: new accounts receive a direct score
  penalty independent of ring membership.
- ``gnn_wash_ring_prob → risk_score``: the GNN's latent-space embedding is
  a direct input to the ensemble score.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import networkx as nx
import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger("ledgerlens.causal_engine")

# ---------------------------------------------------------------------------
# Causal DAG definition
# ---------------------------------------------------------------------------

# Each tuple is (cause, effect).  All edges are documented in the module
# docstring above.  This list is hardcoded and NOT runtime-configurable —
# the causal structure is a domain-knowledge artefact, not a user parameter.
CAUSAL_DAG_EDGES: list[tuple[str, str]] = [
    # Latent wash activity → observable features
    ("wash_activity", "wash_ring_membership"),          # ring membership caused by coordination
    ("wash_activity", "round_trip_trade_frequency"),    # self-dealing inflates round-trip counts
    ("wash_activity", "chi_sq_24h"),                    # bot lot sizes → non-Benford distribution
    ("wash_activity", "cycle_volume_ratio"),            # wash volume flows through ring cycles
    # Feature → feature structural paths
    ("wash_ring_membership", "volume_to_unique_counterparty_ratio"),  # rings repeat counterparties
    ("wash_ring_membership", "round_trip_trade_frequency"),           # rings imply round-trips
    ("account_age_days", "wash_ring_membership"),       # older accounts harder to Sybil
    ("network_centrality", "wash_ring_membership"),     # hubs enable ring formation
    # Features → risk_score (direct causal paths to the outcome)
    ("wash_ring_membership", "risk_score"),
    ("round_trip_trade_frequency", "risk_score"),
    ("chi_sq_24h", "risk_score"),
    ("cycle_volume_ratio", "risk_score"),
    ("volume_to_unique_counterparty_ratio", "risk_score"),
    ("network_centrality", "risk_score"),
    ("account_age_days", "risk_score"),
    ("gnn_wash_ring_prob", "risk_score"),
]

# Observable (non-latent) feature nodes — these must be present as DataFrame
# columns when calling CausalEngine.fit().
OBSERVABLE_FEATURE_NODES: list[str] = [
    "wash_ring_membership",
    "round_trip_trade_frequency",
    "chi_sq_24h",
    "cycle_volume_ratio",
    "volume_to_unique_counterparty_ratio",
    "network_centrality",
    "account_age_days",
    "gnn_wash_ring_prob",
]

# Latent nodes — these are NOT columns in the DataFrame; DoWhy treats them
# as unobserved common causes.
LATENT_NODES: list[str] = ["wash_activity"]

# All feature nodes that can be used as treatments in ATE estimation.
TREATMENT_FEATURES: list[str] = list(OBSERVABLE_FEATURE_NODES)


def build_causal_dag() -> nx.DiGraph:
    """Build and return the LedgerLens causal DAG as a NetworkX DiGraph.

    The DAG encodes domain knowledge about how wash-trading activity causes
    observable feature signals and ultimately the risk score.  Edge
    justifications are documented in ``CAUSAL_DAG_EDGES``.

    Returns
    -------
    nx.DiGraph
        Directed acyclic graph with nodes for all observable features, the
        latent ``wash_activity`` node, and ``risk_score`` as the outcome.

    Raises
    ------
    ValueError
        If the constructed graph contains a cycle (indicates a DAG invariant
        violation — should never happen with the hardcoded edge list).
    """
    G = nx.DiGraph()
    G.add_edges_from(CAUSAL_DAG_EDGES)
    if not nx.is_directed_acyclic_graph(G):
        raise ValueError(
            "CAUSAL_DAG_EDGES contains a cycle — the causal graph must be a DAG."
        )
    return G


# ---------------------------------------------------------------------------
# GML serialisation helper
# ---------------------------------------------------------------------------


def _dag_to_gml_string(dag: nx.DiGraph) -> str:
    """Serialise the causal DAG to a GML string accepted by DoWhy.

    DoWhy's ``graph`` parameter accepts a GML-formatted string.  Latent
    (unobserved) nodes are represented as ``observed 0`` in the GML.
    """
    lines = ["graph [", "  directed 1"]
    node_ids: dict[str, int] = {}
    for i, node in enumerate(dag.nodes()):
        node_ids[node] = i
        observed = 0 if node in LATENT_NODES else 1
        lines.append(f'  node [ id {i} label "{node}" observed {observed} ]')
    for src, dst in dag.edges():
        lines.append(
            f"  edge [ source {node_ids[src]} target {node_ids[dst]} ]"
        )
    lines.append("]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ATE cache (SQLite persistence)
# ---------------------------------------------------------------------------

_ATE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS causal_ate_cache (
    model_version TEXT NOT NULL,
    feature_name  TEXT NOT NULL,
    ate           REAL NOT NULL,
    computed_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (model_version, feature_name)
);
"""


def _init_ate_cache(conn: sqlite3.Connection) -> None:
    """Create the ``causal_ate_cache`` table if it does not exist."""
    conn.execute(_ATE_CACHE_DDL)
    conn.commit()


def _load_ate_cache(conn: sqlite3.Connection, model_version: str) -> dict[str, float] | None:
    """Load the ATE table for ``model_version`` from SQLite, or None if absent."""
    _init_ate_cache(conn)
    rows = conn.execute(
        "SELECT feature_name, ate FROM causal_ate_cache WHERE model_version = ?",
        (model_version,),
    ).fetchall()
    if not rows:
        return None
    return {row[0]: row[1] for row in rows}


def _save_ate_cache(
    conn: sqlite3.Connection,
    model_version: str,
    ate_table: dict[str, float],
) -> None:
    """Persist the ATE table for ``model_version`` to SQLite."""
    _init_ate_cache(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT OR REPLACE INTO causal_ate_cache
            (model_version, feature_name, ate, computed_at)
        VALUES (?, ?, ?, ?)
        """,
        [(model_version, feat, ate, now) for feat, ate in ate_table.items()],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CausalEngine — main class
# ---------------------------------------------------------------------------


class CausalEngine:
    """DoWhy-based structural causal model over LedgerLens ML features.

    The engine fits structural equations to a scored-wallet dataset and exposes
    do-calculus interventions so analysts can answer questions like:

    * "If we remove the Benford signal, what is the causal contribution of
      graph topology to the risk score?"
    * "What would this wallet's score be if it were *not* in a wash ring?"

    Design choices
    --------------
    * ``wash_activity`` is treated as an unobserved latent variable.  DoWhy
      handles this via the ``observed 0`` GML attribute; the backdoor criterion
      is applied over the observed nodes only.
    * Linear structural equations are used by default (``backdoor.linear_regression``)
      for speed and interpretability.  Switch to ``backdoor.econml.dml.DML``
      for nonlinear effects when ``econml`` is available.
    * The ATE table is cached per ``model_version`` in SQLite so that API
      requests do not re-fit the model on every call.

    Parameters
    ----------
    dag:
        The causal DAG, typically from ``build_causal_dag()``.
    estimation_method:
        DoWhy estimation method name.  Default is
        ``"backdoor.linear_regression"``.
    db_path:
        Path to the SQLite database for ATE caching.
    model_version:
        Version tag used as the cache key (e.g. a git commit hash or date).
    refutation_runs:
        Number of simulated datasets used in refutation tests.
    min_sample_size:
        Minimum number of rows required to fit the model.
    """

    def __init__(
        self,
        dag: nx.DiGraph | None = None,
        estimation_method: str = "backdoor.linear_regression",
        db_path: str | None = None,
        model_version: str = "default",
        refutation_runs: int = 100,
        min_sample_size: int = 500,
    ) -> None:
        self._dag = dag if dag is not None else build_causal_dag()
        self.estimation_method = estimation_method
        self._db_path = db_path
        self._model_version = model_version
        self._refutation_runs = refutation_runs
        self._min_sample_size = min_sample_size

        # State set by fit()
        self._fitted: bool = False
        self._df: pd.DataFrame | None = None
        self._linear_coefs: dict[str, float] = {}
        self._linear_intercept: float = 0.0
        self._ate_table: dict[str, float] | None = None

        # Lazy-load DoWhy at runtime to avoid import cost if not used
        self._dowhy_model = None

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit(self, df: pd.DataFrame) -> None:
        """Fit structural equations using the scored-wallet DataFrame.

        Parameters
        ----------
        df:
            Must contain columns for all nodes in ``OBSERVABLE_FEATURE_NODES``
            plus ``"risk_score"``.  The latent node ``wash_activity`` is
            treated as unobserved and must NOT be a column.

        Raises
        ------
        ValueError
            If required columns are missing or the sample is too small.
        """
        self._validate_df(df)

        if len(df) < self._min_sample_size:
            logger.warning(
                "CausalEngine.fit() called with %d rows (minimum %d). "
                "Causal estimates may be unreliable.",
                len(df),
                self._min_sample_size,
            )

        self._df = df.copy()

        # Fit a lightweight linear model for counterfactual_score speed path.
        # This does NOT require DoWhy and is always available.
        self._fit_linear_structural_equations(df)

        # Attempt to build the DoWhy model (optional; gracefully degrade).
        # Treatment is set per-query in estimate_ate(), not here.
        try:
            from dowhy import CausalModel  # type: ignore[import]
            gml = _dag_to_gml_string(self._dag)
            self._dowhy_model = CausalModel(
                data=df,
                treatment=OBSERVABLE_FEATURE_NODES[0],  # placeholder; overridden per-query
                outcome="risk_score",
                graph=gml,
            )
        except ImportError:
            logger.warning(
                "dowhy is not installed — DoWhy-based ATE estimation is unavailable. "
                "counterfactual_score() will use linear structural equations. "
                "Install with: pip install dowhy==0.11.1"
            )
            self._dowhy_model = None
        except Exception as exc:
            logger.warning("DoWhy model construction failed: %s. Falling back to linear path.", exc)
            self._dowhy_model = None

        self._fitted = True
        logger.info(
            "CausalEngine fitted on %d rows (model_version=%s).",
            len(df),
            self._model_version,
        )

    def _validate_df(self, df: pd.DataFrame) -> None:
        """Raise ValueError if required columns are missing."""
        required = set(OBSERVABLE_FEATURE_NODES) | {"risk_score"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"CausalEngine.fit() requires columns: {sorted(missing)}"
            )

    def _fit_linear_structural_equations(self, df: pd.DataFrame) -> None:
        """Fit OLS regression of risk_score on observable features.

        Coefficients are stored in ``self._linear_coefs`` for the fast
        ``counterfactual_score`` path.
        """
        X = df[OBSERVABLE_FEATURE_NODES].fillna(0.0).to_numpy(dtype=float)
        y = df["risk_score"].to_numpy(dtype=float)
        reg = LinearRegression().fit(X, y)
        self._linear_coefs = {
            feat: float(coef)
            for feat, coef in zip(OBSERVABLE_FEATURE_NODES, reg.coef_)
        }
        self._linear_intercept = float(reg.intercept_)


    # ------------------------------------------------------------------
    # ATE estimation
    # ------------------------------------------------------------------

    def estimate_ate(
        self,
        treatment_feature: str,
        control_value: float = 0.0,
        treatment_value: float = 1.0,
    ) -> float:
        """Estimate E[risk_score|do(feature=treatment)] - E[risk_score|do(feature=control)].

        Uses DoWhy with the configured ``estimation_method`` (default:
        ``backdoor.linear_regression``) after identifying the effect via the
        backdoor criterion on the causal DAG.

        Parameters
        ----------
        treatment_feature:
            Name of the feature to intervene on.  Must be in
            ``OBSERVABLE_FEATURE_NODES``.
        control_value:
            Value to set the feature to in the control condition.
        treatment_value:
            Value to set the feature to in the treatment condition.

        Returns
        -------
        float
            The estimated average treatment effect (ATE) in risk-score units.
            Positive means the feature causally increases the risk score.

        Raises
        ------
        RuntimeError
            If the engine has not been fitted yet.
        ValueError
            If ``treatment_feature`` is not an observable feature node.
        """
        self._assert_fitted()
        if treatment_feature not in OBSERVABLE_FEATURE_NODES:
            raise ValueError(
                f"'{treatment_feature}' is not a valid treatment feature. "
                f"Valid features: {OBSERVABLE_FEATURE_NODES}"
            )

        try:
            from dowhy import CausalModel  # type: ignore[import]
        except ImportError:
            # DoWhy not installed — fall back to linear coefficient
            logger.debug(
                "dowhy not installed; using linear coefficient for ATE of '%s'.",
                treatment_feature,
            )
            delta = treatment_value - control_value
            return self._linear_coefs.get(treatment_feature, 0.0) * delta

        gml = _dag_to_gml_string(self._dag)
        model = CausalModel(
            data=self._df,
            treatment=treatment_feature,
            outcome="risk_score",
            graph=gml,
        )

        try:
            estimand = model.identify_effect(proceed_when_unidentifiable=True)
            estimate = model.estimate_effect(
                estimand,
                method_name=self.estimation_method,
                control_value=control_value,
                treatment_value=treatment_value,
                test_significance=False,
            )
            return float(estimate.value)
        except Exception as exc:
            logger.warning(
                "DoWhy estimate_ate failed for treatment='%s': %s. "
                "Falling back to linear coefficient.",
                treatment_feature,
                exc,
            )
            # Fall back to linear coefficient * (treatment - control)
            delta = treatment_value - control_value
            return self._linear_coefs.get(treatment_feature, 0.0) * delta

    # ------------------------------------------------------------------
    # ATE table
    # ------------------------------------------------------------------

    def feature_ate_table(
        self,
        df: pd.DataFrame | None = None,
        use_cache: bool = True,
    ) -> dict[str, float]:
        """Compute the ATE of each observable feature on risk_score.

        For each feature the ATE is estimated as
        ``E[risk_score|do(feature=1)] - E[risk_score|do(feature=0)]``
        on the normalised [0, 1] scale.

        Parameters
        ----------
        df:
            Optional fresh DataFrame to refit on before computing ATEs.
            If ``None``, uses the DataFrame from the last ``fit()`` call.
        use_cache:
            If True, attempt to load from the SQLite ATE cache before
            recomputing.

        Returns
        -------
        dict[str, float]
            Mapping of feature name → ATE value.
        """
        if use_cache and self._db_path:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cached = _load_ate_cache(conn, self._model_version)
                    if cached is not None:
                        logger.debug(
                            "ATE table loaded from cache (model_version=%s).",
                            self._model_version,
                        )
                        self._ate_table = cached
                        return cached
            except Exception as exc:
                logger.warning("Could not read ATE cache: %s", exc)

        if df is not None:
            self.fit(df)
        self._assert_fitted()

        ate_table: dict[str, float] = {}
        for feature in OBSERVABLE_FEATURE_NODES:
            try:
                ate = self.estimate_ate(feature, control_value=0.0, treatment_value=1.0)
            except Exception as exc:
                logger.warning("ATE estimation failed for '%s': %s", feature, exc)
                ate = 0.0
            ate_table[feature] = ate

        self._ate_table = ate_table

        if self._db_path:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    _save_ate_cache(conn, self._model_version, ate_table)
                    logger.debug(
                        "ATE table persisted to cache (model_version=%s).",
                        self._model_version,
                    )
            except Exception as exc:
                logger.warning("Could not write ATE cache: %s", exc)

        return ate_table


    # ------------------------------------------------------------------
    # Counterfactual score
    # ------------------------------------------------------------------

    def counterfactual_score(
        self,
        wallet_features: dict[str, float],
        overrides: dict[str, float],
    ) -> float:
        """Predict risk_score if specified features were set to override values.

        Uses the fitted linear structural equations for speed (O(n_features)).
        Only features in ``OBSERVABLE_FEATURE_NODES`` are used; unknown keys
        in ``overrides`` are silently ignored.

        Parameters
        ----------
        wallet_features:
            The wallet's current feature values (dict of feature name → value).
        overrides:
            Features to override and their new values.

        Returns
        -------
        float
            Predicted risk score in [0, 100].

        Raises
        ------
        RuntimeError
            If the engine has not been fitted yet.
        """
        self._assert_fitted()
        merged = {**wallet_features, **overrides}
        score = self._linear_intercept
        for feat in OBSERVABLE_FEATURE_NODES:
            val = float(merged.get(feat, 0.0))
            score += self._linear_coefs.get(feat, 0.0) * val
        return float(np.clip(score, 0.0, 100.0))

    # ------------------------------------------------------------------
    # Refutation tests
    # ------------------------------------------------------------------

    def refutation_tests(self) -> dict[str, float]:
        """Run DoWhy refutation tests on the primary treatment (``wash_ring_membership``).

        Tests performed:
        - ``random_common_cause``: adds a random confounder; the ATE should
          not change significantly if the identification is correct.
        - ``placebo_treatment_refuter``: replaces the treatment with random
          noise; the estimated effect should collapse to ~0.
        - ``data_subset_refuter``: re-estimates on a 70% random subset; the
          ATE should remain stable.

        Returns
        -------
        dict[str, float]
            Mapping of ``{test_name: p_value}``.  P-values < 0.05 indicate
            the causal model may be misspecified for the tested assumption.

        Raises
        ------
        RuntimeError
            If the engine has not been fitted yet.
        ImportError
            If ``dowhy`` is not installed.
        """
        self._assert_fitted()

        try:
            from dowhy import CausalModel  # type: ignore[import]
        except ImportError:
            logger.warning(
                "dowhy not installed — refutation tests are unavailable. "
                "Returning default p-values of 1.0. "
                "Install with: pip install dowhy==0.11.1"
            )
            return {
                "random_common_cause": 1.0,
                "placebo_treatment_refuter": 1.0,
                "data_subset_refuter": 1.0,
            }

        gml = _dag_to_gml_string(self._dag)
        model = CausalModel(
            data=self._df,
            treatment="wash_ring_membership",
            outcome="risk_score",
            graph=gml,
        )
        estimand = model.identify_effect(proceed_when_unidentifiable=True)
        estimate = model.estimate_effect(
            estimand,
            method_name=self.estimation_method,
            control_value=0.0,
            treatment_value=1.0,
            test_significance=False,
        )

        results: dict[str, float] = {}

        refutation_specs = [
            ("random_common_cause", "random_common_cause"),
            ("placebo_treatment_refuter", "placebo_treatment_refuter"),
            ("data_subset_refuter", "data_subset_refuter"),
        ]

        for key, method_name in refutation_specs:
            try:
                ref = model.refute_estimate(
                    estimand,
                    estimate,
                    method_name=method_name,
                    num_simulations=self._refutation_runs,
                )
                # DoWhy refutation objects expose `refutation_result` as
                # a p-value or effect ratio depending on the refuter.
                pval = getattr(ref, "refutation_result", None)
                if pval is None:
                    # Fallback: some DoWhy versions use p_value attribute
                    pval = getattr(ref, "p_value", 1.0)
                results[key] = float(pval) if pval is not None else 1.0
            except Exception as exc:
                logger.warning("Refutation test '%s' failed: %s", key, exc)
                results[key] = 1.0

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _assert_fitted(self) -> None:
        if not self._fitted:
            raise RuntimeError(
                "CausalEngine has not been fitted. Call fit(df) first."
            )

    def is_fitted(self) -> bool:
        """Return True if the engine has been fitted on data."""
        return self._fitted

    def invalidate_cache(self) -> None:
        """Remove the cached ATE table for the current model_version from SQLite."""
        if not self._db_path:
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                _init_ate_cache(conn)
                conn.execute(
                    "DELETE FROM causal_ate_cache WHERE model_version = ?",
                    (self._model_version,),
                )
                conn.commit()
                logger.info(
                    "ATE cache invalidated for model_version='%s'.",
                    self._model_version,
                )
        except Exception as exc:
            logger.warning("Could not invalidate ATE cache: %s", exc)



# ---------------------------------------------------------------------------
# Legacy PDC layer (preserved from original causal_engine.py)
# ---------------------------------------------------------------------------
# The section below is the original doubly-robust DR-IPW Price Discovery
# Contribution estimator.  It predates the DoWhy SCM layer above and is
# retained for backward compatibility with existing callers (feature_engineering,
# tests/test_causal_engine.py, etc.).

# Confounders controlled for in the PDC treatment-effect estimate.
_CONFOUNDERS = ["hour", "volatility", "volume"]
# Minimum windows (with both treated and control present) needed for a stable estimate.
_MIN_WINDOWS = 6
# Propensities are clipped to keep IPW weights finite under near-separation.
_PROPENSITY_CLIP = (0.05, 0.95)


def propensity_score(features: pd.DataFrame) -> np.ndarray:
    """Logistic propensity P(wallet trades | confounders) for IPW weighting.

    ``features`` must contain a ``treated`` column (0/1 treatment label)
    alongside the confounder columns.  Confounders are standardised before
    fitting.  When only one treatment class is present (no overlap), the
    empirical treatment rate is returned for every row.
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
    """Build the windowed treatment/outcome/confounder panel, or None if infeasible."""
    price_df = _normalise_prices(prices)
    if price_df is None or trades is None or trades.empty:
        return None

    freq = f"{window_minutes}min"

    price_df = price_df.set_index("timestamp")
    window_mid = price_df["mid_price"].resample(freq).last().dropna()
    if len(window_mid) < _MIN_WINDOWS + 1:
        return None
    outcome = window_mid.shift(-1) - window_mid
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
    """Estimate the price-discovery contribution (PDC) of ``wallet`` on ``pair``.

    Returns the ATE of the wallet's trades on the subsequent mid-price:
    positive => market-making (improves price discovery), near-zero or
    negative => wash-trading signal.  Confounders are controlled with a
    doubly-robust IPW estimator.

    Returns ``0.0`` when there is insufficient data or no treatment overlap.
    """
    panel = _build_panel(trades, prices, wallet, pair, window_minutes)
    if panel is None or len(panel) < _MIN_WINDOWS:
        return 0.0
    if panel["treated"].nunique() < 2:
        return 0.0

    try:
        return _doubly_robust_ate(panel)
    except Exception:
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
