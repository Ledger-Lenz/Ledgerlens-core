"""Model Card generation for compliance and auditing purposes.

Implements the Model Cards for Model Reporting pattern, with a Datasheet for
Datasets section covering the training data.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class DatasheetSection:
    """Datasheet section covering training data details."""
    dataset_source: str = "ingestion.synthetic_data"
    n_samples: int = 0
    class_balance_pre_smote: dict[str, float] = field(default_factory=dict)
    class_balance_post_smote: dict[str, float] = field(default_factory=dict)
    imbalance_strategy: str = "SMOTE"
    feature_count: int = 0
    generation_params: dict = field(default_factory=dict)


@dataclass
class ModelCard:
    """Complete Model Card for a trained model version."""
    model_name: str
    version: str
    trained_at: str
    mlflow_run_id: str | None = None
    hyperparameters: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    top_shap_features: list[dict] = field(default_factory=list)
    stability_vs_previous: dict | None = None
    fairness_summary: dict | None = None
    datasheet: DatasheetSection = field(default_factory=DatasheetSection)
    known_limitations: list[str] = field(default_factory=list)
    intended_use: str = (
        "This model is intended to detect wash trading activity on Stellar DEXs, "
        "using behavioral features, Benford's Law analysis, and SHAP values for interpretability."
    )
    out_of_scope_uses: list[str] = field(default_factory=lambda: [
        "Use as a sole decision-making tool for regulatory enforcement without human review",
        "Use on non-Stellar blockchains without retraining",
        "Use for real-time blocking of trades without additional safeguards"
    ])


def generate_model_card(
    model_name: str,
    version: str,
    model_dir: str | None = None
) -> ModelCard:
    """Generate a ModelCard by pulling data from training metadata and files.

    Args:
        model_name: Name of the model (e.g. "random_forest")
        version: Version string
        model_dir: Optional model directory override (uses settings.model_dir by default)

    Returns:
        A populated ModelCard instance
    """
    model_dir = model_dir or settings.model_dir

    # --- Load training metadata ---
    metadata_path = os.path.join(model_dir, "training_metadata.json")
    metadata: dict[str, Any] = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
        except Exception as e:
            logger.warning("Failed to load training_metadata.json: %s", e)

    # --- Load training reference data for datasheet ---
    training_ref_path = os.path.join(model_dir, "training_reference.csv")
    datasheet = DatasheetSection()
    if os.path.exists(training_ref_path):
        try:
            df = pd.read_csv(training_ref_path)
            datasheet.n_samples = len(df)
            if "label" in df.columns:
                label_counts = df["label"].value_counts(normalize=True)
                datasheet.class_balance_pre_smote = {
                    str(k): float(v) for k, v in label_counts.to_dict().items()
                }
            datasheet.feature_count = len([c for c in df.columns if c not in ["label", "wallet"]])
        except Exception as e:
            logger.warning("Failed to load training_reference.csv: %s", e)

    # --- Load SHAP importances ---
    top_shap = []
    shap_importances = metadata.get("shap_importances", {})
    if model_name in shap_importances:
        top_shap = shap_importances[model_name]

    # --- Prepare metrics ---
    model_metrics = metadata.get("model_metrics", {}).get(model_name, {})

    # --- Create ModelCard ---
    trained_at = metadata.get("training_timestamp", datetime.now(timezone.utc).isoformat())

    return ModelCard(
        model_name=model_name,
        version=version,
        trained_at=trained_at,
        mlflow_run_id=metadata.get("mlflow_run_id"),
        hyperparameters=metadata.get("hyperparameters", {}).get(model_name, {}),
        metrics=model_metrics,
        top_shap_features=top_shap,
        stability_vs_previous=metadata.get("stability_vs_previous"),
        datasheet=datasheet,
        known_limitations=[
            "Model performance may degrade under heavy adversarial evasion",
            "Requires sufficient trade history (minimum 10 trades recommended)",
            "Not validated for use outside of Stellar DEXs"
        ]
    )


def render_markdown(card: ModelCard) -> str:
    """Render a ModelCard as a Markdown string.

    Args:
        card: The ModelCard instance to render

    Returns:
        Markdown string
    """
    lines = [
        f"# {card.model_name.replace('_', ' ').title()} - Version {card.version}",
        "",
        f"*Generated on: {datetime.now(timezone.utc).isoformat()}*",
        "",
        "## Model Details",
        "",
        f"- **Model Name**: {card.model_name}",
        f"- **Version**: {card.version}",
        f"- **Trained At**: {card.trained_at}",
    ]
    if card.mlflow_run_id:
        lines.append(f"- **MLflow Run ID**: {card.mlflow_run_id}")

    lines.extend([
        "",
        "## Intended Use",
        "",
        card.intended_use,
        "",
        "## Out of Scope Uses",
        ""
    ])
    for use_case in card.out_of_scope_uses:
        lines.append(f"- {use_case}")

    lines.extend([
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ])
    for key, value in sorted(card.metrics.items()):
        if isinstance(value, float):
            lines.append(f"| {key.replace('_', ' ').title()} | {value:.4f} |")
        else:
            lines.append(f"| {key.replace('_', ' ').title()} | {value} |")

    lines.extend([
        "",
        "## Top Features (SHAP)",
        "",
        "| Rank | Feature | Mean Absolute SHAP |",
        "|------|---------|--------------------|",
    ])
    for feat in card.top_shap_features:
        lines.append(f"| {feat.get('rank', '-')} | {feat.get('feature', '-')} | {feat.get('mean_abs_shap', '-'):.4f} |")

    if card.stability_vs_previous:
        lines.extend([
            "",
            "## Stability vs Previous Version",
            "",
            f"```json\n{json.dumps(card.stability_vs_previous, indent=2)}\n```",
        ])

    if card.fairness_summary:
        lines.extend([
            "",
            "## Fairness & Bias",
            "",
            f"```json\n{json.dumps(card.fairness_summary, indent=2)}\n```",
        ])

    lines.extend([
        "",
        "## Datasheet for Datasets",
        "",
        f"- **Source**: {card.datasheet.dataset_source}",
        f"- **Number of Samples**: {card.datasheet.n_samples}",
        f"- **Feature Count**: {card.datasheet.feature_count}",
        f"- **Imbalance Strategy**: {card.datasheet.imbalance_strategy}",
    ])

    if card.datasheet.class_balance_pre_smote:
        lines.extend([
            "",
            "### Class Balance (Pre-SMOTE)",
            "",
            "| Class | Proportion |",
            "|-------|------------|",
        ])
        for cls, prop in sorted(card.datasheet.class_balance_pre_smote.items()):
            lines.append(f"| {cls} | {prop:.2%} |")

    lines.extend([
        "",
        "## Known Limitations",
        ""
    ])
    for limitation in card.known_limitations:
        lines.append(f"- {limitation}")

    return "\n".join(lines)


def render_pdf(card: ModelCard, output_path: str | None = None) -> bytes | None:
    """Render a ModelCard as PDF (optional, requires weasyprint).

    Args:
        card: ModelCard to render
        output_path: Optional path to write PDF file to

    Returns:
        PDF bytes if weasyprint is available, None otherwise
    """
    if not settings.model_card_pdf_enabled:
        return None

    try:
        import weasyprint
    except ImportError:
        logger.warning("weasyprint not installed; skipping PDF generation")
        return None

    md = render_markdown(card)
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{card.model_name} - v{card.version}</title>
        <style>
            body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; }}
            h1 {{ color: #1a3a5c; }}
            h2 {{ color: #2c5282; margin-top: 2rem; }}
            table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
            th, td {{ border: 1px solid #ccc; padding: 0.5rem; text-align: left; }}
            th {{ background: #f0f4f8; }}
            code {{ background: #f4f4f4; padding: 0.1rem 0.3rem; border-radius: 3px; }}
        </style>
    </head>
    <body>
        {md.replace('# ', '<h1>').replace('## ', '<h2>').replace('### ', '<h3>')}
    </body>
    </html>
    """
    pdf_bytes = weasyprint.HTML(string=html).write_pdf()

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
        logger.info("Wrote PDF model card to %s", output_path)

    return pdf_bytes
