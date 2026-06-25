"""Self-contained compliance audit report generator.

Produces a human-readable HTML (and optionally PDF) report explaining why a
specific wallet received a high risk score on a given date — including feature
values, SHAP attributions, model version, and data provenance.

Every report is read-only and idempotent: the same ``(wallet, date)`` always
produces the same report.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config.settings import settings
from detection.benford_engine import compute_benford_metrics
from detection.feature_engineering import FEATURE_NAMES
from detection.model_registry import get_current_version
from detection.storage import (
    get_feature_vector_for_wallet,
    get_score_history,
    get_shap_values,
)

logger = logging.getLogger("ledgerlens.compliance_report")

_FEATURE_DESCRIPTIONS: dict[str, str] = {
    "benford_chi_square_1h": "First-digit chi-square statistic (1h window)",
    "benford_chi_square_4h": "First-digit chi-square statistic (4h window)",
    "benford_chi_square_24h": "First-digit chi-square statistic (24h window)",
    "benford_chi_square_7d": "First-digit chi-square statistic (7d window)",
    "benford_chi_square_30d": "First-digit chi-square statistic (30d window)",
    "benford_mad_1h": "Mean absolute deviation from Benford (1h window)",
    "benford_mad_4h": "Mean absolute deviation from Benford (4h window)",
    "benford_mad_24h": "Mean absolute deviation from Benford (24h window)",
    "benford_mad_7d": "Mean absolute deviation from Benford (7d window)",
    "benford_mad_30d": "Mean absolute deviation from Benford (30d window)",
    "benford_max_zscore_1h": "Maximum digit-level z-score (1h window)",
    "benford_max_zscore_4h": "Maximum digit-level z-score (4h window)",
    "benford_max_zscore_24h": "Maximum digit-level z-score (24h window)",
    "benford_max_zscore_7d": "Maximum digit-level z-score (7d window)",
    "benford_max_zscore_30d": "Maximum digit-level z-score (30d window)",
    "counterparty_concentration_ratio": "Share of total volume concentrated in the top counterparty",
    "round_trip_trade_frequency": "Frequency of buy-sell round trips with the same counterparty",
    "self_matching_rate": "Fraction of trades where the wallet trades with itself",
    "order_cancellation_rate": "Fraction of placed orders that are cancelled before execution",
    "volume_to_unique_counterparty_ratio": "Average volume per unique counterparty",
    "intra_minute_clustering_coefficient": "Degree of trade clustering within single minutes",
    "off_hours_activity_ratio": "Fraction of trades occurring outside regular market hours (UTC 00:00-06:00)",
    "volume_spike_frequency": "Frequency of abnormal volume bursts relative to the wallet's baseline",
    "funding_source_similarity_score": "Similarity of funding source patterns to known wash rings",
    "network_centrality": "Wallet centrality in the trade relationship graph",
    "account_age_days": "Number of days since the Stellar account was created",
    "wash_ring_membership": "Binary: account belongs to a detected circular trading ring",
    "wash_ring_size": "Number of accounts in the detected wash ring (0 if not a member)",
    "cycle_volume_ratio": "Fraction of total volume circulating within the ring",
    "timing_tightness_score": "How tightly coordinated trades are within the ring",
    "cross_pair_activity_count": "Number of distinct asset pairs the wallet trades",
    "cross_pair_synchrony_score": "Degree of synchronous trading across different pairs",
    "cross_pair_burst_overlap_ratio": "Fraction of burst periods overlapping across pairs",
    "shared_wallet_cluster_size": "Number of wallets sharing similar cross-pair patterns",
    "cross_pair_volume_concentration": "Volume concentration across traded pairs",
    "pool_trade_ratio": "Fraction of volume routed through liquidity pools vs orderbook",
    "pool_round_trip_ratio": "Frequency of round-trip trades through AMM pools",
    "pool_share_concentration": "Concentration of pool share ownership for this wallet",
    "atomic_self_payment_ratio": "Fraction of path payments where source == destination",
    "avg_path_hop_count": "Average number of intermediate hops in path payments",
    "path_cycle_volume_ratio": "Fraction of path-payment volume in source==destination cycles",
    "path_cycle_count_24h": "Number of multi-hop cycles the wallet participates in (24h)",
    "path_cycle_volume_24h": "Total volume flowing through detected path cycles (24h)",
    "path_cycle_timing_regularity": "Regularity of inter-cycle timing for path cycles",
    "pdc_5m": "Price discovery contribution measured over 5-minute windows",
    "pdc_1h": "Price discovery contribution measured over 1-hour windows",
    "price_discovery_contribution": "Blended price discovery contribution score",
}


def _feature_description(name: str) -> str:
    return _FEATURE_DESCRIPTIONS.get(name, name.replace("_", " ").title())


class ComplianceReportGenerator:
    """Generates self-contained HTML/PDF compliance audit reports.

    Parameters
    ----------
    wallet:
        Stellar wallet address (G...).
    date:
        ISO 8601 date string (YYYY-MM-DD) for the report date.
    output_path:
        Path to write the output file (e.g. ``report.html``).
    db_path:
        Optional path to the SQLite database. Defaults to ``settings.db_path``.
    """

    def __init__(
        self,
        wallet: str,
        date: str,
        output_path: str,
        db_path: Optional[str] = None,
    ) -> None:
        self.wallet = wallet
        self.date = date
        self.output_path = output_path
        self.db_path = db_path or settings.db_path

    @property
    def _date_start(self) -> str:
        return f"{self.date}T00:00:00"

    @property
    def _date_end(self) -> str:
        return f"{self.date}T23:59:59"

    def _gather_risk_scores(self) -> list[dict]:
        """Fetch risk scores for the wallet on the report date."""
        return get_score_history(
            wallet=self.wallet,
            start=self._date_start,
            end=self._date_end,
            db_path=self.db_path,
        )

    def _gather_feature_vector(self) -> Optional[dict]:
        """Fetch the cached feature vector for the wallet on the report date."""
        scores = self._gather_risk_scores()
        asset_pairs = {s["asset_pair"] for s in scores}
        for pair in sorted(asset_pairs):
            fv = get_feature_vector_for_wallet(
                wallet=self.wallet, asset_pair=pair, db_path=self.db_path
            )
            if fv:
                return fv
        return None

    def _gather_shap(self) -> dict[str, list[dict]]:
        """Fetch SHAP explanations for each asset pair."""
        scores = self._gather_risk_scores()
        asset_pairs = {s["asset_pair"] for s in scores}
        result: dict[str, list[dict]] = {}
        for pair in sorted(asset_pairs):
            shap = get_shap_values(
                wallet=self.wallet, asset_pair=pair, db_path=self.db_path
            )
            if shap:
                result[pair] = shap
        return result

    def _compute_benford(self) -> dict:
        """Compute Benford's Law metrics from trade history."""
        from detection.storage import _connect

        amounts: list[float] = []
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT base_amount FROM risk_scores
                JOIN trades ON risk_scores.wallet = trades.base_account
                WHERE risk_scores.wallet = ?
                  AND trades.ledger_close_time >= ?
                  AND trades.ledger_close_time <= ?
                """,
                (self.wallet, self._date_start, self._date_end),
            ).fetchall()
            amounts = [float(r[0]) for r in rows if r[0]]
        if not amounts:
            return {"chi_square": 0.0, "mad": 0.0, "sample_size": 0}
        return compute_benford_metrics(amounts)

    def _get_model_info(self) -> dict:
        """Get model version and training date metadata."""
        model_dir = settings.model_dir
        metadata_path = os.path.join(model_dir, "training_metadata.json")
        version_map: dict[str, Optional[str]] = {}
        for name in ("random_forest", "xgboost", "lightgbm"):
            version_map[name] = get_current_version(name, model_dir)
        training_date = None
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path) as f:
                    meta = json.load(f)
                training_date = meta.get("training_timestamp")
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "versions": version_map,
            "training_date": training_date,
        }

    def _get_data_provenance(self) -> dict:
        """Get Horizon data provenance (cursor range)."""
        cursor_path = settings.cursor_path
        cursor = None
        if os.path.exists(cursor_path):
            try:
                with open(cursor_path) as f:
                    cursor = f.read().strip()
            except OSError:
                pass
        return {
            "horizon_url": settings.horizon_url,
            "last_cursor": cursor,
            "network": settings.network,
        }

    def generate_html(self) -> str:
        """Generate the HTML report and return the rendered string."""
        scores = self._gather_risk_scores()
        feature_vector = self._gather_feature_vector()
        shap_by_pair = self._gather_shap()
        benford = self._compute_benford()
        model_info = self._get_model_info()
        provenance = self._get_data_provenance()

        if scores:
            peak_score = max(s["score"] for s in scores)
            peak_pair = max(scores, key=lambda s: s["score"])
        else:
            peak_score = 0
            peak_pair = None

        top_features: list[dict] = []
        if feature_vector:
            fv_features = {k: v for k, v in feature_vector.items() if k in FEATURE_NAMES}
            sorted_fv = sorted(fv_features.items(), key=lambda x: abs(x[1]), reverse=True)
            for name, value in sorted_fv[:5]:
                top_features.append({
                    "name": name,
                    "value": round(float(value), 6),
                    "description": _feature_description(name),
                })

        top_shap: list[dict] = []
        for pair, shap_list in shap_by_pair.items():
            for item in shap_list:
                top_shap.append({
                    "pair": pair,
                    "feature": item["feature"],
                    "shap_value": round(float(item["shap_value"]), 6),
                    "description": _feature_description(item["feature"]),
                })
        top_shap.sort(key=lambda x: abs(x["shap_value"]), reverse=True)
        top_shap = top_shap[:5]

        trade_timeline: list[dict] = []
        if scores:
            for s in scores:
                trade_timeline.append({
                    "timestamp": s["timestamp"],
                    "asset_pair": s["asset_pair"],
                    "score": s["score"],
                    "benford_flag": s["benford_flag"],
                    "ml_flag": s["ml_flag"],
                })

        risk_level = "LOW"
        if peak_score >= 70:
            risk_level = "HIGH"
        elif peak_score >= 40:
            risk_level = "MEDIUM"
        if peak_score >= 90:
            risk_level = "CRITICAL"

        try:
            from jinja2 import Environment, FileSystemLoader
            template_dir = os.path.join(os.path.dirname(__file__), "templates")
            env = Environment(loader=FileSystemLoader(template_dir))
            template = env.get_template("report.html")
            html = template.render(
                wallet=self.wallet,
                date=self.date,
                generated_at=datetime.now(timezone.utc).isoformat(),
                peak_score=peak_score,
                peak_pair=peak_pair["asset_pair"] if peak_pair else "N/A",
                risk_level=risk_level,
                score_lower=peak_pair.get("score_lower") if peak_pair else None,
                score_upper=peak_pair.get("score_upper") if peak_pair else None,
                total_scores=len(scores),
                feature_vector=feature_vector,
                top_features=top_features,
                top_shap=top_shap,
                benford=benford,
                trade_timeline=trade_timeline,
                model_info=model_info,
                provenance=provenance,
            )
        except ImportError:
            html = self._generate_html_fallback(
                scores, peak_score, risk_level, top_features, top_shap,
                benford, model_info, provenance,
            )
        return html

    def _generate_html_fallback(
        self, scores, peak_score, risk_level, top_features, top_shap,
        benford, model_info, provenance,
    ) -> str:
        """Minimal inline HTML when Jinja2 is not available."""
        rows = "".join(
            f"<tr><td>{s['timestamp']}</td><td>{s['asset_pair']}</td>"
            f"<td>{s['score']}</td><td>{s['benford_flag']}</td>"
            f"<td>{s['ml_flag']}</td></tr>"
            for s in scores[:50]
        )
        feature_rows = "".join(
            f"<tr><td>{f['name']}</td><td>{f['value']}</td>"
            f"<td>{f['description']}</td></tr>"
            for f in top_features
        )
        shap_rows = "".join(
            f"<tr><td>{s['feature']}</td><td>{s['pair']}</td>"
            f"<td>{s['shap_value']}</td><td>{s['description']}</td></tr>"
            for s in top_shap
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Compliance Report - {self.wallet}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2em;color:#1a1a1a}}
h1,h2,h3{{color:#1a3a5c}}table{{border-collapse:collapse;width:100%;margin:1em 0}}
th,td{{border:1px solid #ccc;padding:8px;text-align:left}}
th{{background:#f0f4f8}}.risk-HIGH{{color:#c00}}.risk-CRITICAL{{color:#900}}
.risk-LOW{{color:#090}}.risk-MEDIUM{{color:#960}}
.summary{{background:#f8f9fa;padding:1em;border-radius:8px;margin:1em 0}}
</style></head>
<body>
<h1>LedgerLens Compliance Audit Report</h1>
<div class="summary">
<p><strong>Wallet:</strong> {self.wallet}</p>
<p><strong>Date:</strong> {self.date}</p>
<p><strong>Generated:</strong> {datetime.now(timezone.utc).isoformat()}</p>
</div>
<h2>Executive Summary</h2>
<p>Peak Risk Score: <strong class="risk-{risk_level}">{peak_score}/100 ({risk_level})</strong></p>
<h2>Top-5 SHAP Features</h2>
<table><tr><th>Feature</th><th>Value</th><th>Description</th></tr>{feature_rows}</table>
<h2>SHAP Attributions</h2>
<table><tr><th>Feature</th><th>Pair</th><th>SHAP Value</th><th>Description</th></tr>{shap_rows}</table>
<h2>Benford Analysis</h2>
<p>Chi-Square: {benford.get("chi_square", 0):.4f} | MAD: {benford.get("mad", 0):.4f} | Samples: {benford.get("sample_size", 0)}</p>
<h2>Trade Timeline</h2>
<table><tr><th>Timestamp</th><th>Pair</th><th>Score</th><th>Benford</th><th>ML</th></tr>{rows}</table>
<h2>Model Info</h2>
<p>Training Date: {model_info.get("training_date", "N/A")}</p>
<p>Versions: {json.dumps(model_info.get("versions", {}))}</p>
<h2>Data Provenance</h2>
<p>Horizon URL: {provenance.get("horizon_url", "N/A")}</p>
<p>Network: {provenance.get("network", "N/A")}</p>
<p>Last Cursor: {provenance.get("last_cursor", "N/A")}</p>
</body></html>"""

    def generate_pdf(self) -> Optional[str]:
        """Generate a PDF version of the report using WeasyPrint, if available.

        Returns the path to the PDF file, or ``None`` if WeasyPrint is not
        installed.
        """
        try:
            import weasyprint
        except ImportError:
            logger.warning("weasyprint not installed; skipping PDF generation")
            return None

        html = self.generate_html()
        pdf_path = self.output_path.replace(".html", ".pdf")
        weasyprint.HTML(string=html).write_pdf(pdf_path)
        logger.info("Wrote PDF report to %s", pdf_path)
        return pdf_path

    def generate(self) -> str:
        """Generate the report (HTML, and PDF if possible).

        Returns the path to the generated HTML file.
        """
        html = self.generate_html()
        with open(self.output_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Wrote HTML report to %s", self.output_path)

        try:
            self.generate_pdf()
        except Exception as exc:
            logger.warning("PDF generation failed: %s", exc)

        return self.output_path
