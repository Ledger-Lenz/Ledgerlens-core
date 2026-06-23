"""Template-based Suspicious Activity Report (SAR) narrative generator.

FinCEN SAR Form 111 requires a plain-English narrative describing the suspicious
activity.  This module renders that narrative from LedgerLens risk intelligence
*without* any LLM dependency, so the output is deterministic, auditable and free
of hallucinated content — every value in the narrative traces back to a stored
score or alert.

See `detection.compliance_exporter` for the package assembly that consumes this.
"""

from __future__ import annotations

SAR_TEMPLATE = """Between {start_date} and {end_date}, wallet {wallet} received a LedgerLens Risk Score \
of {peak_score}/100 (peak), indicating {risk_level} risk of wash trading activity.

The following anomalies were detected:
{alert_bullets}

Trade volume during the period: {volume_xlm:,.0f} XLM across {n_pairs} asset pairs.
Counterparty cluster size: {cluster_size} accounts.
Benford chi-square statistic: {chi_sq:.2f} (p={chi_p:.4f}).
"""


def risk_level_from_score(score: float) -> str:
    """Map a 0-100 risk score onto the FATF-aligned categorical risk level."""
    if score >= 90:
        return "CRITICAL"
    if score >= 70:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


def _format_alert_bullets(alerts: list[dict]) -> str:
    """Render alerts as a bulleted list; never returns an empty/placeholder line."""
    if not alerts:
        return "  - No discrete manipulation alerts were recorded in this period."

    bullets: list[str] = []
    for alert in alerts:
        alert_type = str(alert.get("alert_type", "UNKNOWN")).replace("_", " ").title()
        detail = alert.get("detail") or {}
        ts = alert.get("timestamp", "unknown time")
        descriptor = alert_type
        if "profit_xlm" in detail:
            descriptor += f" (attacker profit {float(detail['profit_xlm']):,.2f} XLM)"
        elif "cycle_volume" in detail:
            descriptor += f" (cycle volume {float(detail['cycle_volume']):,.2f} XLM)"
        pair = alert.get("asset_pair")
        pair_suffix = f" on {pair}" if pair else ""
        bullets.append(f"  - {descriptor}{pair_suffix} observed at {ts}.")
    return "\n".join(bullets)


def generate_sar_narrative(
    *,
    wallet: str,
    start_date: str,
    end_date: str,
    peak_score: float,
    alerts: list[dict],
    volume_xlm: float,
    n_pairs: int,
    cluster_size: int,
    chi_sq: float,
    chi_p: float,
) -> str:
    """Render the SAR narrative.

    Every template token is bound to a concrete value here, so the returned text
    is guaranteed to contain no unresolved ``{placeholder}`` markers.
    """
    narrative = SAR_TEMPLATE.format(
        wallet=wallet,
        start_date=start_date,
        end_date=end_date,
        peak_score=int(round(peak_score)),
        risk_level=risk_level_from_score(peak_score),
        alert_bullets=_format_alert_bullets(alerts),
        volume_xlm=float(volume_xlm),
        n_pairs=int(n_pairs),
        cluster_size=int(cluster_size),
        chi_sq=float(chi_sq),
        chi_p=float(chi_p),
    )
    return narrative
