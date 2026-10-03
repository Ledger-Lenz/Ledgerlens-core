"""Tests for pluggable regulatory export formats (issue #1029)."""

import pytest

from detection import compliance_formats as cf
from detection.compliance_exporter import IVMSRiskField

WALLET = "G" + "A" * 55


@pytest.fixture(autouse=True)
def _fake_risk(monkeypatch):
    field = IVMSRiskField(
        ledgerlens_score=87.0,
        risk_level="HIGH",
        alert_types=["WASH_TRADE"],
        score_timestamp="2026-09-01T00:00:00+00:00",
        evidence_hash="a" * 64,
    )
    monkeypatch.setattr(cf, "build_ivms_risk_field", lambda wallet, db_path=None: field)


def test_both_formats_available():
    assert {"ivms101", "goaml"} <= set(cf.available_formats())


@pytest.mark.parametrize("fmt", ["ivms101", "goaml"])
def test_export_validates_against_format_schema(fmt):
    payload = cf.export_risk_assessment(WALLET, fmt=fmt)
    assert cf.validate_schema(payload, cf.get_format(fmt).schema) == []


def test_goaml_payload_contents():
    payload = cf.export_risk_assessment(WALLET, fmt="goaml", rentity_id=42)
    party = payload["activity"]["report_parties"]["report_party"][0]
    assert payload["rentity_id"] == 42
    assert party["account"]["account"] == WALLET
    assert party["significance"] == 9
    assert payload["report_indicators"]["indicator"] == ["WASH_TRADE"]


def test_invalid_payload_rejected():
    with pytest.raises(cf.ExportValidationError):
        cf.get_format("goaml").validate({"rentity_id": -1})


def test_unknown_format_rejected():
    with pytest.raises(ValueError):
        cf.export_risk_assessment(WALLET, fmt="nope")


def test_new_format_plugin_needs_no_core_changes():
    @cf.register_format
    class Dummy(cf.ExportFormat):
        name = "dummy"
        schema = {"type": "object", "required": ["wallet"]}

        def build(self, wallet, db_path=None, **options):
            return {"wallet": wallet}

    try:
        assert cf.export_risk_assessment(WALLET, fmt="dummy") == {"wallet": WALLET}
    finally:
        cf._REGISTRY.pop("dummy")
