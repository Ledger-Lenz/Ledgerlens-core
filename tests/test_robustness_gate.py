import json

import pytest

from detection import model_registry as mr
from detection.robustness_eval import RobustnessReport


def _report(radius: float) -> RobustnessReport:
    return RobustnessReport(
        model_version="abc12345",
        asr={},
        mean_map=0.0,
        p95_map=0.0,
        certified_radius=radius,
        n_samples=10,
        epsilon=0.1,
    )


def test_below_threshold_cannot_be_promoted_without_override(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(mr, "save_versioned_model", lambda *a, **k: saved.append(a))
    with pytest.raises(mr.RobustnessGateError):
        mr.promote_model(object(), "xgboost", "abc12345", str(tmp_path), _report(0.01))
    assert saved == []


def test_above_threshold_is_promoted(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(mr, "save_versioned_model", lambda *a, **k: saved.append(a))
    mr.promote_model(object(), "xgboost", "abc12345", str(tmp_path), _report(0.2))
    assert len(saved) == 1


def test_override_is_audited(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(mr, "save_versioned_model", lambda *a, **k: saved.append(a))
    override = mr.RobustnessOverride(approved_by="risk-lead", justification="hotfix")
    mr.promote_model(object(), "xgboost", "abc12345", str(tmp_path), _report(0.01), override=override)
    assert len(saved) == 1
    entry = json.loads((tmp_path / mr.ROBUSTNESS_OVERRIDE_LOG).read_text().strip())
    assert entry["approved_by"] == "risk-lead"
    assert entry["model_version"] == "abc12345"


def test_override_requires_approver_and_justification():
    with pytest.raises(ValueError):
        mr.RobustnessOverride(approved_by=" ", justification="x")
