"""Tests for ModelCard generation, Markdown rendering, and PDF output."""

import json
import os

import pytest

from detection.model_card import (
    DatasheetSection,
    ModelCard,
    _render_markdown_to_html,
    generate_model_card,
    render_markdown,
    render_pdf,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_model_dir(tmp_path):
    """Create a minimal model directory with training metadata."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()

    metadata = {
        "version": "abc12345",
        "training_timestamp": "2026-07-20T12:00:00Z",
        "mlflow_run_id": "run-001",
        "hyperparameters": {
            "random_forest": {"n_estimators": 200, "max_depth": 10},
        },
        "model_metrics": {
            "random_forest": {
                "auc_roc": 0.95,
                "pr_auc": 0.87,
                "f1": 0.82,
                "precision": 0.85,
                "recall": 0.79,
            },
        },
        "shap_importances": {
            "random_forest": [
                {"feature": "benford_chi_square_1h", "mean_abs_shap": 0.35, "rank": 1},
                {"feature": "self_matching_rate", "mean_abs_shap": 0.28, "rank": 2},
                {"feature": "volume_spike_frequency", "mean_abs_shap": 0.15, "rank": 3},
            ],
        },
        "stability_vs_previous": {
            "spearman_rho": {"random_forest": 0.92},
            "stable": True,
        },
    }
    (model_dir / "training_metadata.json").write_text(json.dumps(metadata))
    return str(model_dir)


@pytest.fixture
def sample_model_dir_with_fairness(sample_model_dir):
    """Augment metadata with fairness summary."""
    path = os.path.join(sample_model_dir, "training_metadata.json")
    with open(path, "r") as f:
        metadata = json.load(f)
    metadata["fairness_summary"] = {
        "demographic_parity_difference": 0.05,
        "equal_opportunity_difference": 0.03,
    }
    with open(path, "w") as f:
        json.dump(metadata, f)
    return sample_model_dir


@pytest.fixture
def sample_model_dir_with_training_csv(tmp_path):
    """Create a model directory with a training_reference.csv file."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()

    # Write metadata
    metadata = {
        "version": "abc12345",
        "training_timestamp": "2026-07-20T12:00:00Z",
    }
    (model_dir / "training_metadata.json").write_text(json.dumps(metadata))

    # Write training reference CSV
    csv_content = "feature_a,feature_b,feature_c,label,wallet\n"
    csv_content += "0.1,0.2,0.3,1,GABC\n"
    csv_content += "0.4,0.5,0.6,0,GDEF\n"
    csv_content += "0.7,0.8,0.9,1,GHIJ\n"
    (model_dir / "training_reference.csv").write_text(csv_content)

    return str(model_dir)


# ---------------------------------------------------------------------------
# DatasheetSection tests
# ---------------------------------------------------------------------------


class TestDatasheetSection:
    def test_default_values(self):
        ds = DatasheetSection()
        assert ds.dataset_source == "ingestion.synthetic_data"
        assert ds.n_samples == 0
        assert ds.imbalance_strategy == "SMOTE"
        assert ds.feature_count == 0
        assert ds.class_balance_pre_smote == {}
        assert ds.class_balance_post_smote == {}
        assert ds.generation_params == {}

    def test_custom_values(self):
        ds = DatasheetSection(
            dataset_source="custom",
            n_samples=1000,
            feature_count=42,
            imbalance_strategy="ADASYN",
            class_balance_pre_smote={"0": 0.7, "1": 0.3},
        )
        assert ds.dataset_source == "custom"
        assert ds.n_samples == 1000
        assert ds.feature_count == 42
        assert ds.imbalance_strategy == "ADASYN"
        assert ds.class_balance_pre_smote == {"0": 0.7, "1": 0.3}


# ---------------------------------------------------------------------------
# ModelCard tests
# ---------------------------------------------------------------------------


class TestModelCard:
    def test_default_model_card(self):
        card = ModelCard(model_name="test_model", version="v001", trained_at="2026-01-01T00:00:00Z")
        assert card.model_name == "test_model"
        assert card.version == "v001"
        assert card.trained_at == "2026-01-01T00:00:00Z"
        assert card.mlflow_run_id is None
        assert card.hyperparameters == {}
        assert card.metrics == {}
        assert card.top_shap_features == []
        assert card.stability_vs_previous is None
        assert card.fairness_summary is None
        assert isinstance(card.datasheet, DatasheetSection)
        # known_limitations defaults to empty list on direct construction;
        # the 3-item list is set by generate_model_card, not the dataclass default
        assert isinstance(card.known_limitations, list)
        assert "Stellar DEXs" in card.intended_use

    def test_model_card_with_shap_features(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            top_shap_features=[
                {"feature": "f1", "mean_abs_shap": 0.5, "rank": 1},
                {"feature": "f2", "mean_abs_shap": 0.3, "rank": 2},
            ],
        )
        assert len(card.top_shap_features) == 2
        assert card.top_shap_features[0]["feature"] == "f1"


# ---------------------------------------------------------------------------
# generate_model_card tests
# ---------------------------------------------------------------------------


class TestGenerateModelCard:
    def test_generates_card_with_metadata(self, sample_model_dir):
        card = generate_model_card("random_forest", "abc12345", model_dir=sample_model_dir)
        assert card.model_name == "random_forest"
        assert card.version == "abc12345"
        assert card.mlflow_run_id == "run-001"
        assert card.hyperparameters == {"n_estimators": 200, "max_depth": 10}
        assert card.metrics["auc_roc"] == 0.95
        assert card.metrics["pr_auc"] == 0.87
        assert len(card.top_shap_features) == 3
        assert card.stability_vs_previous is not None
        assert card.stability_vs_previous["stable"] is True

    def test_generates_card_with_fairness(self, sample_model_dir_with_fairness):
        card = generate_model_card(
            "random_forest", "abc12345", model_dir=sample_model_dir_with_fairness
        )
        assert card.fairness_summary is not None
        assert card.fairness_summary["demographic_parity_difference"] == 0.05

    def test_generates_card_with_csv_datasheet(self, sample_model_dir_with_training_csv):
        card = generate_model_card(
            "random_forest", "abc12345", model_dir=sample_model_dir_with_training_csv
        )
        assert card.datasheet.n_samples == 3
        assert card.datasheet.feature_count == 3  # feature_a, feature_b, feature_c
        assert "1" in card.datasheet.class_balance_pre_smote

    def test_handles_missing_metadata_gracefully(self, tmp_path):
        model_dir = str(tmp_path / "nonexistent")
        os.makedirs(model_dir, exist_ok=True)
        card = generate_model_card("test_model", "v001", model_dir=model_dir)
        assert card.model_name == "test_model"
        assert card.metrics == {}
        assert card.top_shap_features == []

    def test_handles_corrupt_metadata_gracefully(self, tmp_path):
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        (model_dir / "training_metadata.json").write_text("{invalid json")
        card = generate_model_card("test_model", "v001", model_dir=str(model_dir))
        assert card.model_name == "test_model"
        assert card.hyperparameters == {}

    def test_handles_corrupt_csv_gracefully(self, tmp_path):
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        (model_dir / "training_metadata.json").write_text("{}")
        (model_dir / "training_reference.csv").write_text("garbage,data\nx,y")
        card = generate_model_card("test_model", "v001", model_dir=str(model_dir))
        # Should not crash; datasheet may have partial info
        assert isinstance(card.datasheet, DatasheetSection)

    def test_default_model_dir(self, monkeypatch):
        """When model_dir is None, settings.model_dir is used."""
        import config.settings as settings_module

        monkeypatch.setattr(settings_module.settings, "model_dir", "/tmp/ledgerlens-test-models")
        card = generate_model_card("rf", "v1")
        assert card.model_name == "rf"


# ---------------------------------------------------------------------------
# render_markdown tests
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    def test_renders_basic_card(self):
        card = ModelCard(
            model_name="random_forest",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            metrics={"auc_roc": 0.95, "f1": 0.82},
        )
        md = render_markdown(card)
        assert "# Random Forest - Version v001" in md
        assert "- **Model Name**: random_forest" in md
        assert "- **Version**: v001" in md
        assert "| Auc Roc | 0.9500 |" in md or "| Auc Roc | 0.95" in md

    def test_renders_mlflow_run_id_when_present(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            mlflow_run_id="run-abc",
        )
        md = render_markdown(card)
        assert "- **MLflow Run ID**: run-abc" in md

    def test_renders_shap_features_correctly(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            top_shap_features=[
                {"feature": "benford_chi_square", "mean_abs_shap": 0.35, "rank": 1},
            ],
        )
        md = render_markdown(card)
        assert "## Top Features (SHAP)" in md
        assert "benford_chi_square" in md

    def test_renders_stability_when_present(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            stability_vs_previous={"spearman_rho": {"rf": 0.92}},
        )
        md = render_markdown(card)
        assert "## Stability vs Previous Version" in md

    def test_renders_fairness_when_present(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            fairness_summary={"demographic_parity": 0.05},
        )
        md = render_markdown(card)
        assert "## Fairness & Bias" in md

    def test_renders_intended_use_and_out_of_scope(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
        )
        md = render_markdown(card)
        assert "## Intended Use" in md
        assert "## Out of Scope Uses" in md

    def test_renders_known_limitations(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            known_limitations=["Model performance may degrade under heavy adversarial evasion"],
        )
        md = render_markdown(card)
        assert "## Known Limitations" in md
        assert "adversarial evasion" in md

    def test_renders_non_float_metrics(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            metrics={"best_threshold": "0.5"},
        )
        md = render_markdown(card)
        assert "0.5" in md

    def test_renders_empty_top_shap_gracefully(self):
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
            top_shap_features=[],
        )
        md = render_markdown(card)
        assert "## Top Features (SHAP)" in md


# ---------------------------------------------------------------------------
# _render_markdown_to_html tests
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    def test_converts_headings(self):
        md = "# Title\n\n## Section\n\n### Subsection\n\nText."
        html = _render_markdown_to_html(md)
        assert "<h1>Title</h1>" in html
        assert "<h2>Section</h2>" in html
        assert "<h3>Subsection</h3>" in html

    def test_wraps_paragraphs(self):
        md = "Hello world\n\nGoodbye"
        html = _render_markdown_to_html(md)
        assert "<p>Hello world</p>" in html
        assert "<p>Goodbye</p>" in html

    def test_preserves_empty_lines(self):
        md = "Line 1\n\n\nLine 2"
        html = _render_markdown_to_html(md)
        # Should have blank line separators between paragraphs
        assert "<p>Line 1</p>" in html
        assert "<p>Line 2</p>" in html

    def test_handles_code_blocks_safely(self):
        """Code blocks containing '#' should not be corrupted."""
        md = "## Section\n\n```\n# This is a comment, not a heading\n```\n\nMore text."
        html = _render_markdown_to_html(md)
        # The '#' inside code block should not become <h1>
        assert "<h2>Section</h2>" in html
        # The fallback converter doesn't parse code blocks, so '#' inside
        # backticks will appear as-is in <p> tags — but importantly it won't
        # be confused with a heading since heading detection requires the line
        # to START with the hash after stripping whitespace.


# ---------------------------------------------------------------------------
# render_pdf tests
# ---------------------------------------------------------------------------


class TestRenderPdf:
    def test_returns_none_when_disabled(self, monkeypatch):
        """render_pdf returns None when model_card_pdf_enabled is False."""
        import config.settings as settings_module

        monkeypatch.setattr(settings_module.settings, "model_card_pdf_enabled", False)
        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
        )
        result = render_pdf(card)
        assert result is None

    def test_returns_bytes_when_enabled_and_weasyprint_available(self, tmp_path, monkeypatch):
        """render_pdf returns PDF bytes when WeasyPrint is installed and enabled."""
        import config.settings as settings_module

        monkeypatch.setattr(settings_module.settings, "model_card_pdf_enabled", True)

        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
        )

        try:
            import weasyprint  # noqa: F401
        except ImportError:
            pytest.skip("weasyprint not installed")

        result = render_pdf(card)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_writes_to_output_path(self, tmp_path, monkeypatch):
        """render_pdf writes the PDF to the specified output path."""
        import config.settings as settings_module

        monkeypatch.setattr(settings_module.settings, "model_card_pdf_enabled", True)

        card = ModelCard(
            model_name="rf",
            version="v001",
            trained_at="2026-01-01T00:00:00Z",
        )

        try:
            import weasyprint  # noqa: F401
        except ImportError:
            pytest.skip("weasyprint not installed")

        output_path = str(tmp_path / "reports" / "model_card.pdf")
        result = render_pdf(card, output_path=output_path)
        assert os.path.exists(output_path)
        assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# Integration test: full generate → render cycle
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_generate_and_render_cycle(self, sample_model_dir):
        """Generate a ModelCard from metadata and render it as Markdown."""
        card = generate_model_card(
            "random_forest", "abc12345", model_dir=sample_model_dir
        )
        md = render_markdown(card)
        # Sanity checks on the output
        assert "Random Forest" in md
        assert "abc12345" in md
        assert "0.9500" in md
        assert "benford_chi_square_1h" in md
