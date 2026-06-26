"""Regulatory compliance export layer.

Packages LedgerLens risk intelligence into the deliverable formats required by
financial regulators and by VASPs running LedgerLens as part of an AML program:

* **FATF Travel Rule (IVMS 101)** — :func:`augment_ivms_payload` injects a
  LedgerLens fraud-risk block into an existing IVMS 101 originator/beneficiary
  payload.
* **Suspicious Activity Report (FinCEN Form 111)** — :func:`generate_sar_package`
  assembles a self-contained, integrity-verifiable ZIP of evidence (narrative,
  alerts, score history, relationship graph, model explanations).
* **Audit trail** — :func:`get_audit_trail` returns a single timestamped log of
  every recorded event touching a wallet, suitable for a legal hold.

Nothing here calls out to an LLM: narratives are template-rendered (see
:mod:`detection.sar_narrative`) so exports are deterministic and auditable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import networkx as nx
from scipy import stats

from config.settings import settings
from detection.benford_engine import compute_benford_metrics
from detection.sar_narrative import generate_sar_narrative, risk_level_from_score
from detection.storage import (
    _connect,
    count_compliance_exports_since,
    get_alerts,
    get_latest_scores,
    get_score_history,
    get_shap_values,
    init_db,
    log_compliance_export,
)


class ComplianceRateLimitExceeded(RuntimeError):
    """Raised when the configured hourly compliance-export rate limit is hit."""


class ComplianceScoreTooLow(RuntimeError):
    """Raised when a wallet's risk score is below the configured SAR minimum."""

    def __init__(self, score: float, minimum: int):
        self.score = score
        self.minimum = minimum
        super().__init__(f"risk score {score} is below the SAR minimum ({minimum})")


@dataclass
class IVMSRiskField:
    """LedgerLens risk augmentation block for an IVMS 101 Travel Rule payload."""

    ledgerlens_score: float
    risk_level: str          # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    alert_types: list[str]   # e.g. ["WASH_TRADE", "PATH_PAYMENT_CYCLE"]
    score_timestamp: str     # ISO 8601
    evidence_hash: str       # SHA-256 of the score commitment (links to ZK proof)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence_hash(wallet: str, score: float, score_timestamp: str) -> str:
    """Deterministic SHA-256 commitment over the scored fact.

    Mirrors the score-commitment hash used by the on-chain ZK proof so a
    Travel-Rule recipient can cross-reference the two.
    """
    commitment = f"{wallet}|{int(round(score))}|{score_timestamp}"
    return hashlib.sha256(commitment.encode("utf-8")).hexdigest()


def hash_wallet(wallet: str) -> str:
    """Stable SHA-256 digest identifying ``wallet`` in the audit log.

    Distinct from :func:`_evidence_hash`: this identifies the wallet itself
    for audit correlation, not a specific scored fact, so the compliance
    export audit trail never stores a plaintext wallet address.
    """
    return hashlib.sha256(wallet.encode("utf-8")).hexdigest()


def build_ivms_risk_field(wallet: str, db_path: str | None = None) -> IVMSRiskField:
    """Build the LedgerLens risk block for ``wallet`` from its latest scores/alerts."""
    scores = get_latest_scores(wallet=wallet, db_path=db_path)
    if scores:
        peak = max(s.score for s in scores)
        latest_ts = max(s.timestamp for s in scores).isoformat()
    else:
        peak = 0
        latest_ts = _now_iso()

    alert_types = sorted({a["alert_type"] for a in get_alerts(wallet=wallet, db_path=db_path)})

    return IVMSRiskField(
        ledgerlens_score=float(peak),
        risk_level=risk_level_from_score(peak),
        alert_types=alert_types,
        score_timestamp=latest_ts,
        evidence_hash=_evidence_hash(wallet, peak, latest_ts),
    )


def augment_ivms_payload(ivms_json: dict, wallet: str, db_path: str | None = None) -> dict:
    """Inject LedgerLens risk fields into an existing IVMS 101 JSON payload.

    Returns a new dict (the input is not mutated) with a
    ``ledgerLensRiskAssessment`` member carrying the :class:`IVMSRiskField`.
    """
    field = build_ivms_risk_field(wallet, db_path=db_path)
    augmented = json.loads(json.dumps(ivms_json))  # deep copy, JSON-safe
    augmented["ledgerLensRiskAssessment"] = asdict(field)
    return augmented


# ---------------------------------------------------------------------------
# SAR evidence package
# ---------------------------------------------------------------------------


def _gather_trade_context(
    wallet: str, start: str, end: str, db_path: str | None
) -> tuple[list[float], set[str], float]:
    """Collect transaction amounts, counterparties and traded volume for a wallet.

    Reads path payments and AMM pool trades the wallet participated in within the
    inclusive ``[start, end]`` window.  Tables that happen to be empty simply
    contribute nothing.
    """
    amounts: list[float] = []
    counterparties: set[str] = set()
    volume = 0.0

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT source_account, destination_account, source_amount
            FROM path_payments
            WHERE (source_account = ? OR destination_account = ?)
              AND timestamp >= ? AND timestamp <= ?
            """,
            (wallet, wallet, start, end),
        ).fetchall()
        for src, dst, amount in rows:
            amounts.append(float(amount))
            volume += float(amount)
            other = dst if src == wallet else src
            if other and other != wallet:
                counterparties.add(other)

        rows = conn.execute(
            """
            SELECT base_amount, pool_id
            FROM liquidity_pool_trades
            WHERE base_account = ? AND timestamp >= ? AND timestamp <= ?
            """,
            (wallet, start, end),
        ).fetchall()
        for amount, pool_id in rows:
            amounts.append(float(amount))
            volume += float(amount)
            if pool_id:
                counterparties.add(f"pool:{pool_id}")

    return amounts, counterparties, volume


def _benford_chi(amounts: list[float]) -> tuple[float, float]:
    """Return the Benford first-digit chi-square statistic and its p-value.

    Falls back to ``(0.0, 1.0)`` when there are too few observations to test.
    """
    metrics = compute_benford_metrics(amounts)
    chi_sq = float(metrics["chi_square"])
    if metrics["sample_size"] < 1:
        return 0.0, 1.0
    # First-digit Benford has 9 categories -> 8 degrees of freedom.
    p_value = float(stats.chi2.sf(chi_sq, df=8))
    return chi_sq, p_value


def _build_relationship_graph(wallet: str, counterparties: set[str], alerts: list[dict]) -> nx.DiGraph:
    """Build the account relationship graph used for FinCEN Form 111."""
    graph = nx.DiGraph()
    graph.add_node(wallet, role="subject")
    for cp in counterparties:
        graph.add_node(cp, role="counterparty")
        graph.add_edge(wallet, cp, relationship="trade")
    for alert in alerts:
        victim = (alert.get("detail") or {}).get("victim")
        if victim and victim != wallet:
            graph.add_node(victim, role="victim")
            graph.add_edge(wallet, victim, relationship=str(alert.get("alert_type", "alert")).lower())
    return graph


def _collect_shap(wallet: str, asset_pairs: set[str], db_path: str | None) -> dict:
    """Gather cached SHAP explanations for each asset pair the wallet traded."""
    explanations: dict[str, list[dict]] = {}
    for pair in sorted(asset_pairs):
        cached = get_shap_values(wallet=wallet, asset_pair=pair, db_path=db_path)
        if cached:
            explanations[pair] = cached
    return explanations


def generate_sar_package(
    wallet: str,
    start_date: str,
    end_date: str,
    output_dir: str,
    db_path: str | None = None,
) -> str:
    """Generate a SAR evidence ZIP archive for ``wallet`` over ``[start, end]``.

    The archive contains:

    - ``sar_narrative.txt`` — auto-generated plain-English narrative.
    - ``evidence/alerts.json`` — all alerts for the wallet in the date range.
    - ``evidence/score_history.csv`` — risk score time series.
    - ``evidence/graph_export.gexf`` — account relationship graph (Form 111).
    - ``evidence/shap_explanations.json`` — model explainability report.
    - ``manifest.json`` — SHA-256 of every included file for integrity checks.

    Returns the path to the generated ZIP file.
    """
    init_db(db_path)
    os.makedirs(output_dir, exist_ok=True)

    score_history = get_score_history(wallet, start_date, end_date, db_path=db_path)
    alerts = get_alerts(wallet=wallet, start=start_date, end=end_date, db_path=db_path)
    amounts, counterparties, volume_xlm = _gather_trade_context(wallet, start_date, end_date, db_path)

    asset_pairs = {row["asset_pair"] for row in score_history}
    peak_score = max((row["score"] for row in score_history), default=0)
    chi_sq, chi_p = _benford_chi(amounts)
    graph = _build_relationship_graph(wallet, counterparties, alerts)
    shap_explanations = _collect_shap(wallet, asset_pairs, db_path)

    # --- Render each artifact into memory ---
    narrative = generate_sar_narrative(
        wallet=wallet,
        start_date=start_date,
        end_date=end_date,
        peak_score=peak_score,
        alerts=alerts,
        volume_xlm=volume_xlm,
        n_pairs=len(asset_pairs),
        cluster_size=graph.number_of_nodes(),
        chi_sq=chi_sq,
        chi_p=chi_p,
    )

    alerts_json = json.dumps(alerts, indent=2, sort_keys=True)

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["timestamp", "asset_pair", "score", "benford_flag", "ml_flag", "confidence"])
    for row in score_history:
        writer.writerow(
            [
                row["timestamp"],
                row["asset_pair"],
                row["score"],
                int(row["benford_flag"]),
                int(row["ml_flag"]),
                row["confidence"],
            ]
        )
    score_history_csv = csv_buffer.getvalue()

    gexf_buffer = io.BytesIO()
    nx.write_gexf(graph, gexf_buffer)
    graph_gexf = gexf_buffer.getvalue()

    shap_json = json.dumps(shap_explanations, indent=2, sort_keys=True)

    # name -> bytes
    files: dict[str, bytes] = {
        "sar_narrative.txt": narrative.encode("utf-8"),
        "evidence/alerts.json": alerts_json.encode("utf-8"),
        "evidence/score_history.csv": score_history_csv.encode("utf-8"),
        "evidence/graph_export.gexf": graph_gexf,
        "evidence/shap_explanations.json": shap_json.encode("utf-8"),
    }

    manifest = {
        "wallet": wallet,
        "start_date": start_date,
        "end_date": end_date,
        "generated_at": _now_iso(),
        "files": {
            name: {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in files.items()
        },
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    safe_wallet = wallet[:12]
    zip_name = f"sar_{safe_wallet}_{start_date[:10]}_{end_date[:10]}.zip"
    zip_path = os.path.join(output_dir, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("manifest.json", manifest_bytes)

    return zip_path


# ---------------------------------------------------------------------------
# Compliance-gated export wrappers
#
# These wrap the lower-level generators above with the controls regulators
# expect of an export pipeline: a minimum-risk-score gate before issuing a
# SAR, an hourly rate limit (SAR generation triggers SHAP recomputation and
# graph building, so it isn't free), and an audit log entry per export.
# `dry_run` skips only the audit log write, per its documented limitation:
# it must not be used to produce a regulatory-submission copy of the export.
# ---------------------------------------------------------------------------


def export_sar_package(
    wallet: str,
    start_date: str,
    end_date: str,
    output_dir: str,
    dry_run: bool = False,
    db_path: str | None = None,
) -> str:
    """Compliance-gated wrapper around :func:`generate_sar_package`.

    Raises :class:`ComplianceRateLimitExceeded` if the configured hourly
    export rate limit has been reached, or :class:`ComplianceScoreTooLow` if
    ``wallet``'s current risk score is below ``settings.compliance_sar_min_score``.
    Logs the export to the ``compliance_exports`` audit table unless
    ``dry_run`` is set.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    if count_compliance_exports_since(since, db_path=db_path) >= settings.compliance_export_rate_limit_per_hour:
        raise ComplianceRateLimitExceeded()

    risk = build_ivms_risk_field(wallet, db_path=db_path)
    if risk.ledgerlens_score < settings.compliance_sar_min_score:
        raise ComplianceScoreTooLow(risk.ledgerlens_score, settings.compliance_sar_min_score)

    zip_path = generate_sar_package(wallet, start_date, end_date, output_dir, db_path=db_path)

    if not dry_run:
        log_compliance_export(
            export_type="sar",
            wallet_hash=hash_wallet(wallet),
            risk_score=int(risk.ledgerlens_score),
            dry_run=False,
            db_path=db_path,
        )

    return zip_path


def export_travel_rule(wallet: str, dry_run: bool = False, db_path: str | None = None) -> IVMSRiskField:
    """Compliance-gated wrapper around :func:`build_ivms_risk_field`.

    Logs the export to the ``compliance_exports`` audit table unless
    ``dry_run`` is set. No minimum risk score is enforced here -- the Travel
    Rule applies to qualifying transfer amounts, not risk level.
    """
    field = build_ivms_risk_field(wallet, db_path=db_path)
    if not dry_run:
        log_compliance_export(
            export_type="travel_rule",
            wallet_hash=hash_wallet(wallet),
            risk_score=int(field.ledgerlens_score),
            dry_run=False,
            db_path=db_path,
        )
    return field


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def get_audit_trail(wallet: str, db_path: str | None = None) -> list[dict]:
    """Return a single timestamped audit log of every event touching ``wallet``.

    Aggregates risk scores, typed alerts, on-chain submissions, disputes and
    score overrides into one chronologically ordered list (oldest first),
    suitable for a legal hold.
    """
    init_db(db_path)
    events: list[dict] = []

    with _connect(db_path) as conn:
        for ts, score, asset_pair in conn.execute(
            "SELECT timestamp, score, asset_pair FROM risk_scores WHERE wallet = ?",
            (wallet,),
        ).fetchall():
            events.append(
                {"timestamp": ts, "event_type": "RISK_SCORE", "asset_pair": asset_pair, "score": score}
            )

        for atype, asset_pair, detail_json, ts in conn.execute(
            "SELECT alert_type, asset_pair, detail_json, timestamp FROM alerts WHERE wallet = ?",
            (wallet,),
        ).fetchall():
            events.append(
                {
                    "timestamp": ts,
                    "event_type": "ALERT",
                    "alert_type": atype,
                    "asset_pair": asset_pair,
                    "detail": json.loads(detail_json) if detail_json else {},
                }
            )

        for ts, asset_pair, status, tx_hash in conn.execute(
            "SELECT submitted_at, asset_pair, status, tx_hash FROM on_chain_submissions WHERE wallet = ?",
            (wallet,),
        ).fetchall():
            events.append(
                {
                    "timestamp": ts,
                    "event_type": "ON_CHAIN_SUBMISSION",
                    "asset_pair": asset_pair,
                    "status": status,
                    "tx_hash": tx_hash,
                }
            )

        for dispute_id, asset_pair, status, ts, resolved_at in conn.execute(
            "SELECT dispute_id, asset_pair, status, submitted_at, resolved_at FROM score_disputes WHERE wallet = ?",
            (wallet,),
        ).fetchall():
            events.append(
                {
                    "timestamp": ts,
                    "event_type": "DISPUTE",
                    "dispute_id": dispute_id,
                    "asset_pair": asset_pair,
                    "status": status,
                    "resolved_at": resolved_at,
                }
            )

        for asset_pair, dispute_id, status, ts in conn.execute(
            "SELECT asset_pair, dispute_id, status, recorded_at FROM score_overrides WHERE wallet = ?",
            (wallet,),
        ).fetchall():
            events.append(
                {
                    "timestamp": ts,
                    "event_type": "SCORE_OVERRIDE",
                    "asset_pair": asset_pair,
                    "dispute_id": dispute_id,
                    "status": status,
                }
            )

    events.sort(key=lambda e: e["timestamp"])
    return events
