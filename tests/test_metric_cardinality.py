"""Tests for scripts/check_metric_cardinality.py (#1003)."""

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_metric_cardinality


@pytest.fixture
def root(tmp_path):
    (tmp_path / "monitoring").mkdir()
    shutil.copy(REPO_ROOT / "monitoring" / "cardinality_budget.yml", tmp_path / "monitoring")
    return tmp_path


def _add_metric(root, labels):
    (root / "metrics.py").write_text(
        "from prometheus_client import Counter\n"
        f'm = Counter("ledgerlens_test_total", "Test metric", {labels!r})\n'
    )


def test_repository_is_within_budget():
    assert check_metric_cardinality.main(["--root", str(REPO_ROOT)]) == 0


def test_rejects_per_wallet_label(root):
    _add_metric(root, ["wallet"])
    assert check_metric_cardinality.main(["--root", str(root)]) == 1


def test_rejects_undeclared_label(root):
    _add_metric(root, ["brand_new_label"])
    assert check_metric_cardinality.main(["--root", str(root)]) == 1


def test_rejects_metric_over_series_budget(root):
    _add_metric(root, ["namespace_id", "endpoint", "status_code"])
    assert check_metric_cardinality.main(["--root", str(root)]) == 1


def test_rejects_forbidden_label_in_recording_rule(root):
    (root / "monitoring" / "rules.yml").write_text(
        "groups:\n  - name: g\n    rules:\n"
        "      - record: x\n        expr: sum by (wallet) (rate(y[5m]))\n"
    )
    assert check_metric_cardinality.main(["--root", str(root)]) == 1


def test_accepts_bounded_metric(root):
    _add_metric(root, ["asset_pair", "result"])
    assert check_metric_cardinality.main(["--root", str(root)]) == 0
