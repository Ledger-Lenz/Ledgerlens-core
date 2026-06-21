from cli import robustness_eval


def test_cli_robustness_eval_runs():
    # run a short evaluation to ensure it exits without error
    robustness_eval(epsilon=0.05, steps=5, n_samples=10)
import os

from typer.testing import CliRunner

from cli import app

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
