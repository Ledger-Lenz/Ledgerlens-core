import os
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli import app, robustness_eval
from detection.robustness_eval import RobustnessReport


def _fake_report(**_kwargs):
    """Return a minimal RobustnessReport without running the real pipeline."""
    return RobustnessReport(
        model_version="test",
        asr={"0.05": 0.0, "0.10": 0.0, "0.20": 0.0},
        mean_map=0.0,
        p95_map=0.0,
        certified_radius=0.0,
        n_samples=10,
        epsilon=0.05,
    )


def test_cli_robustness_eval_runs():
    """Verify the CLI command runs end-to-end without error.

    The heavy computation (PGD attacks + randomized smoothing) is exercised by
    tests/test_robustness_eval.py.  Here we only care that the CLI wiring works.

    All imports inside robustness_eval() are lazy/local, so we must patch them
    at their source modules rather than at the 'cli' namespace.
    """
    with patch("detection.robustness_eval.compute_robustness_report", return_value=_fake_report()), \
         patch("detection.model_inference.load_models", return_value={}), \
         patch("ingestion.synthetic_data.generate_synthetic_dataset",
               return_value=([], {}, [], {})), \
         patch("detection.dataset.build_training_dataset", return_value=MagicMock(
             sample=lambda n, random_state=None: MagicMock()
         )):
        robustness_eval(epsilon=0.05, steps=5, n_samples=10)

runner = CliRunner()


def test_generate_data_writes_csvs(tmp_path):
    out_dir = str(tmp_path / "synthetic")
    result = runner.invoke(
        app,
        [
            "generate-data",
            "--out-dir", out_dir,
            "--n-normal-accounts", "3",
            "--n-wash-rings", "1",
            "--ring-size", "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert os.path.exists(os.path.join(out_dir, "trades.csv"))
    assert os.path.exists(os.path.join(out_dir, "order_book_events.csv"))
    assert os.path.exists(os.path.join(out_dir, "labels.csv"))


def test_train_saves_models(tmp_path, monkeypatch):
    model_dir = str(tmp_path / "models")
    monkeypatch.setenv("MODEL_DIR", model_dir)

    import config.settings as settings_module

    object.__setattr__(settings_module.settings, "model_dir", model_dir)

    result = runner.invoke(
        app,
        [
            "train",
            "--n-normal-accounts", "30",
            "--n-wash-rings", "8",
            "--ring-size", "3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert os.path.exists(os.path.join(model_dir, "random_forest.joblib"))
    assert os.path.exists(os.path.join(model_dir, "xgboost.joblib"))
    assert os.path.exists(os.path.join(model_dir, "lightgbm.joblib"))


def test_reweight_dry_run_prints_table_does_not_write(tmp_path, monkeypatch):
    import os

    import config.settings as settings_module

    db_path = str(tmp_path / "test.db")
    model_dir = str(tmp_path / "models")
    os.makedirs(model_dir, exist_ok=True)
    monkeypatch.setenv("LEDGERLENS_DB_PATH", db_path)
    monkeypatch.setenv("MODEL_DIR", model_dir)
    object.__setattr__(settings_module.settings, "db_path", db_path)
    object.__setattr__(settings_module.settings, "model_dir", model_dir)

    result = runner.invoke(app, ["reweight", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "random_forest" in result.output
    assert "xgboost" in result.output
    assert "lightgbm" in result.output
    weights_path = os.path.join(model_dir, "ensemble_weights.json")
    assert not os.path.exists(weights_path), "dry-run must not write ensemble_weights.json"


class TestSignModelsCommand:
    def _make_unsigned_model(self, path):
        import joblib
        from sklearn.ensemble import RandomForestClassifier

        m = RandomForestClassifier(n_estimators=2, random_state=0).fit([[0], [1]], [0, 1])
        joblib.dump(m, path)

    def test_sign_models_backfills_unsigned_artifacts(self, tmp_path, monkeypatch):
        import config.settings as settings_module

        model_dir = str(tmp_path)
        self._make_unsigned_model(os.path.join(model_dir, "random_forest.joblib"))
        object.__setattr__(settings_module.settings, "model_dir", model_dir)

        result = runner.invoke(app, ["sign-models", "--model-dir", model_dir])

        assert result.exit_code == 0, result.output
        assert os.path.exists(os.path.join(model_dir, "random_forest.joblib.sig"))
        assert "Signed 1 file(s)" in result.output

    def test_sign_models_is_idempotent(self, tmp_path, monkeypatch):
        import config.settings as settings_module

        model_dir = str(tmp_path)
        self._make_unsigned_model(os.path.join(model_dir, "random_forest.joblib"))
        object.__setattr__(settings_module.settings, "model_dir", model_dir)

        runner.invoke(app, ["sign-models", "--model-dir", model_dir])
        result = runner.invoke(app, ["sign-models", "--model-dir", model_dir])

        assert result.exit_code == 0, result.output
        assert "Signed 0 file(s)" in result.output
        assert "skipped 1" in result.output

    def test_sign_models_fails_without_key(self, tmp_path, monkeypatch):
        import config.settings as settings_module

        model_dir = str(tmp_path)
        self._make_unsigned_model(os.path.join(model_dir, "random_forest.joblib"))
        object.__setattr__(settings_module.settings, "model_dir", model_dir)
        object.__setattr__(settings_module.settings, "model_signing_key", "")
        monkeypatch.delenv("LEDGERLENS_MODEL_SIGNING_KEY", raising=False)

        result = runner.invoke(app, ["sign-models", "--model-dir", model_dir])

        assert result.exit_code != 0

    def test_save_models_produces_signed_artifacts(self, tmp_path, monkeypatch):
        """save_models signs every .joblib it writes."""
        import config.settings as settings_module
        from sklearn.ensemble import RandomForestClassifier
        from detection.model_training import save_models

        model_dir = str(tmp_path / "models")
        object.__setattr__(settings_module.settings, "model_dir", model_dir)

        dummy = RandomForestClassifier(n_estimators=2, random_state=0).fit([[0], [1]], [0, 1])
        results = {
            name: {"model": dummy, "auc_roc": 0.9, "pr_auc": 0.8, "f1": 0.85}
            for name in ("random_forest", "xgboost", "lightgbm")
        }

        save_models(results, model_dir=model_dir)

        assert os.path.exists(os.path.join(model_dir, "random_forest.joblib.sig"))
        assert os.path.exists(os.path.join(model_dir, "xgboost.joblib.sig"))
        assert os.path.exists(os.path.join(model_dir, "lightgbm.joblib.sig"))
