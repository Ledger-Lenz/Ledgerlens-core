---
title: "Add Experiment Tracking Integration with MLflow for Model Training Runs"
labels: ["difficulty: intermediate", "area: ml", "type: feature"]
assignees: []
---

## Summary
Model training runs in `detection/model_trainer.py` produce no persistent record of hyperparameters, training metrics, or model artifacts. Adding MLflow experiment tracking gives the team a queryable history of all training runs, enabling comparison between runs and one-click model artifact retrieval for deployment.

## Objectives
- [ ] Add `mlflow` to dependencies and configure `MLFLOW_TRACKING_URI` (default: local `./mlruns`)
- [ ] Wrap `model_trainer.py` training loop with `mlflow.start_run()` context
- [ ] Log: hyperparameters, train/val metrics (AUC-ROC, F1, precision, recall), training duration, and dataset hash
- [ ] Log trained model artifact via `mlflow.sklearn.log_model()` for each ensemble component
- [ ] `cli.py train --experiment-name "benford-v2"` sets the MLflow experiment name

## Definition of Done
- [ ] Every `cli.py train` invocation creates an MLflow run with all hyperparameters and metrics
- [ ] Model artifact loadable from MLflow store: `mlflow.sklearn.load_model("runs:/{run_id}/model")`
- [ ] MLflow UI accessible locally via `mlflow ui --port 5001`
- [ ] Integration test verifies run is created with correct parameter keys
