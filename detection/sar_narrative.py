"""Template-based Suspicious Activity Report (SAR) narrative generator.

FinCEN SAR Form 111 requires a plain-English narrative describing the suspicious
activity.  This module renders that narrative from LedgerLens risk intelligence
*without* any LLM dependency, so the output is deterministic, auditable and free
of hallucinated content — every value in the narrative traces back to a stored
score or alert.

See `detection.compliance_exporter` for the package assembly that consumes this.
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

SAR_TEMPLATE = (
    "Between {start_date} and {end_date}, wallet {wallet} received a LedgerLens Risk "
    "Score of {peak_score}/100 (peak), indicating {risk_level} risk of wash trading activity.\n"
    """

The following anomalies were detected:
{alert_bullets}

Trade volume during the period: {volume_xlm:,.0f} XLM across {n_pairs} asset pairs.
Counterparty cluster size: {cluster_size} accounts.
Benford chi-square statistic: {chi_sq:.2f} (p={chi_p:.4f}).
"""
)


def risk_level_from_score(score: float) -> str:
    """Map a 0-100 risk score onto the FATF-aligned categorical risk level."""
    if score >= 90:
        return "CRITICAL"
    if score >= 70:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


def _format_xlm_amount(value: Any) -> str | None:
    """Return a formatted XLM amount, or ``None`` when the value is not numeric."""
    try:
        return f"{float(value):,.2f} XLM"
    except (TypeError, ValueError):
        return None


def _format_alert_bullets(alerts: Sequence[Mapping[str, Any]] | None) -> str:
    """Render alerts as a bulleted list; never returns an empty/placeholder line."""
    if not alerts:
        return "  - No discrete manipulation alerts were recorded in this period."

    bullets: list[str] = []
    for alert in alerts:
        alert_type = str(alert.get("alert_type", "UNKNOWN")).replace("_", " ").title()
        detail = alert.get("detail") or {}
        ts = alert.get("timestamp", "unknown time")
        descriptor = alert_type
        if isinstance(detail, Mapping) and "profit_xlm" in detail:
            amount = _format_xlm_amount(detail["profit_xlm"])
            if amount is not None:
                descriptor += f" (attacker profit {amount})"
        elif isinstance(detail, Mapping) and "cycle_volume" in detail:
            amount = _format_xlm_amount(detail["cycle_volume"])
            if amount is not None:
                descriptor += f" (cycle volume {amount})"
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
    alerts: Sequence[Mapping[str, Any]] | None,
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


# ---------------------------------------------------------------------------
# Mandatory human review
#
# A generated narrative is only ever a *draft*.  Before it may be exported it
# must be approved by a named compliance analyst via `review_sar_narrative`,
# and every export path must obtain the final text through
# `require_approved_narrative`, which refuses unreviewed drafts and reviews
# that were made against a different draft.
# ---------------------------------------------------------------------------


class SARNarrativeNotReviewed(PermissionError):
    """Raised when a SAR narrative is exported without a valid human review."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SARNarrativeReview:
    """Recorded human approval of an auto-generated SAR narrative draft."""

    reviewer: str
    reviewed_at: str
    draft_sha256: str
    final_text: str
    edited: bool
    diff: str
    notes: str = ""

    @property
    def final_sha256(self) -> str:
        return _sha256(self.final_text)

    def to_audit_record(self) -> dict[str, Any]:
        """Serialisable audit record: reviewer, timestamp, hashes and edits."""
        record = asdict(self)
        record.pop("final_text")
        record["final_sha256"] = self.final_sha256
        return record


def review_sar_narrative(
    draft: str,
    reviewer: str,
    *,
    edited_text: str | None = None,
    notes: str = "",
) -> SARNarrativeReview:
    """Record a compliance analyst's approval of ``draft``.

    ``edited_text`` is the analyst's corrected narrative, if they changed the
    draft; the edits are captured as a unified diff for the audit trail.
    """
    if not reviewer or not reviewer.strip():
        raise SARNarrativeNotReviewed("a named reviewer is required to approve a SAR narrative")
    final_text = draft if edited_text is None else edited_text
    if not final_text.strip():
        raise SARNarrativeNotReviewed("an approved SAR narrative must not be empty")
    diff = "".join(
        difflib.unified_diff(
            draft.splitlines(keepends=True),
            final_text.splitlines(keepends=True),
            fromfile="draft",
            tofile="approved",
        )
    )
    return SARNarrativeReview(
        reviewer=reviewer.strip(),
        reviewed_at=datetime.now(timezone.utc).isoformat(),
        draft_sha256=_sha256(draft),
        final_text=final_text,
        edited=final_text != draft,
        diff=diff,
        notes=notes,
    )


def require_approved_narrative(draft: str, review: SARNarrativeReview | None) -> str:
    """Return the exportable narrative text, or raise if it was not reviewed.

    The review must be bound to this exact draft (by SHA-256), so an approval
    of an earlier draft cannot be reused after the underlying data changes.
    """
    if not isinstance(review, SARNarrativeReview):
        raise SARNarrativeNotReviewed("SAR narrative has not been reviewed by a compliance analyst")
    if not review.reviewer.strip():
        raise SARNarrativeNotReviewed("SAR narrative review has no recorded reviewer")
    if review.draft_sha256 != _sha256(draft):
        raise SARNarrativeNotReviewed("SAR narrative review does not match the current draft")
    return review.final_text
