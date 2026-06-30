"""Tests for monitoring/alerts.yml — validates Prometheus alerting rule schema."""

import pytest
import yaml
from pathlib import Path

ALERTS_PATH = Path(__file__).parent.parent / "monitoring" / "alerts.yml"

REQUIRED_ALERTS = {
    "SorobanCircuitBreakerOpen",
    "WebhookDeadLetterBacklog",
    "FeatureDriftDetected",
    "ScoringLatencyHigh",
    "PipelineStalled",
}


@pytest.fixture(scope="module")
def alerts_doc():
    with open(ALERTS_PATH) as f:
        return yaml.safe_load(f)


def _collect_rules(doc):
    rules = []
    for group in doc.get("groups", []):
        rules.extend(group.get("rules", []))
    return rules


def test_alerts_file_is_valid_yaml():
    with open(ALERTS_PATH) as f:
        doc = yaml.safe_load(f)
    assert isinstance(doc, dict)
    assert "groups" in doc


def test_all_5_alert_rules_present(alerts_doc):
    rules = _collect_rules(alerts_doc)
    names = {r["alert"] for r in rules}
    missing = REQUIRED_ALERTS - names
    assert not missing, f"Missing alert rules: {missing}"


def test_each_rule_has_required_fields(alerts_doc):
    rules = _collect_rules(alerts_doc)
    for rule in rules:
        assert "alert" in rule, f"Rule missing 'alert': {rule}"
        assert "expr" in rule, f"Rule {rule.get('alert')} missing 'expr'"
        assert "for" in rule, f"Rule {rule.get('alert')} missing 'for'"
        assert "annotations" in rule, f"Rule {rule.get('alert')} missing 'annotations'"


def test_each_annotation_has_summary(alerts_doc):
    rules = _collect_rules(alerts_doc)
    for rule in rules:
        annotations = rule.get("annotations", {})
        assert "summary" in annotations, f"Rule {rule.get('alert')} missing annotations.summary"


def test_exactly_5_rules(alerts_doc):
    rules = _collect_rules(alerts_doc)
    assert len(rules) == 5, f"Expected 5 alert rules, got {len(rules)}"
