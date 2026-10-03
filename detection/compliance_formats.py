"""Pluggable regulatory export formats.

Each format is an :class:`ExportFormat` plugin registered by name with
:func:`register_format`. :func:`export_risk_assessment` selects a plugin at
export time, builds its payload and validates it against the plugin's JSON
Schema before returning it. Adding a new format only requires a new plugin
class decorated with ``@register_format`` — the core exporter is untouched.

Built-in formats:

* ``ivms101`` — FATF Travel Rule IVMS 101 LedgerLens risk block (the existing
  :func:`detection.compliance_exporter.build_ivms_risk_field` output).
* ``goaml`` — UNODC goAML report (JSON projection of the goAML 4.x ``report``
  element), accepted by FIUs in 60+ jurisdictions for STR/SAR filings.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime, timezone

from detection.compliance_exporter import build_ivms_risk_field


class ExportValidationError(ValueError):
    """Raised when an export payload does not conform to its format schema."""


_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


def validate_schema(instance, schema: dict, path: str = "$") -> list[str]:
    """Validate ``instance`` against a JSON Schema subset.

    Supports ``type``, ``required``, ``properties``, ``additionalProperties``
    (``False`` only), ``items``, ``enum``, ``pattern``, ``minimum``,
    ``maximum`` and ``minItems``. Returns a list of error messages.
    """
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        py_type = _JSON_TYPES[expected]
        if not isinstance(instance, py_type) or (expected != "boolean" and isinstance(instance, bool)):
            return [f"{path}: expected {expected}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if "pattern" in schema and not re.fullmatch(schema["pattern"], instance):
        errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if "minimum" in schema and instance < schema["minimum"]:
        errors.append(f"{path}: {instance} < {schema['minimum']}")
    if "maximum" in schema and instance > schema["maximum"]:
        errors.append(f"{path}: {instance} > {schema['maximum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}: unexpected property {k!r}" for k in instance if k not in props)
        for key, sub in props.items():
            if key in instance:
                errors.extend(validate_schema(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(instance):
                errors.extend(validate_schema(item, schema["items"], f"{path}[{i}]"))
    return errors


class ExportFormat(ABC):
    """Base class for a regulatory export format plugin."""

    name: str
    schema: dict

    @abstractmethod
    def build(self, wallet: str, db_path: str | None = None, **options) -> dict:
        """Build the export payload for ``wallet``."""

    def validate(self, payload: dict) -> None:
        errors = validate_schema(payload, self.schema)
        if errors:
            raise ExportValidationError(f"{self.name} export invalid: " + "; ".join(errors))


_REGISTRY: dict[str, type[ExportFormat]] = {}


def register_format(cls: type[ExportFormat]) -> type[ExportFormat]:
    """Class decorator registering an :class:`ExportFormat` plugin by ``name``."""
    _REGISTRY[cls.name] = cls
    return cls


def available_formats() -> list[str]:
    return sorted(_REGISTRY)


def get_format(name: str) -> ExportFormat:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"Unknown export format {name!r}; available: {available_formats()}") from None


def export_risk_assessment(wallet: str, fmt: str = "ivms101", db_path: str | None = None, **options) -> dict:
    """Build and validate a regulatory export for ``wallet`` in format ``fmt``."""
    plugin = get_format(fmt)
    payload = plugin.build(wallet, db_path=db_path, **options)
    plugin.validate(payload)
    return payload


_RISK_LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
_SHA256 = "[0-9a-f]{64}"


@register_format
class IVMS101Format(ExportFormat):
    """FATF Travel Rule IVMS 101 LedgerLens risk assessment block."""

    name = "ivms101"
    schema = {
        "type": "object",
        "required": ["ledgerlens_score", "risk_level", "alert_types", "score_timestamp", "evidence_hash"],
        "additionalProperties": False,
        "properties": {
            "ledgerlens_score": {"type": "number", "minimum": 0, "maximum": 100},
            "risk_level": {"type": "string", "enum": _RISK_LEVELS},
            "alert_types": {"type": "array", "items": {"type": "string"}},
            "score_timestamp": {"type": "string"},
            "evidence_hash": {"type": "string", "pattern": _SHA256},
        },
    }

    def build(self, wallet: str, db_path: str | None = None, **options) -> dict:
        return asdict(build_ivms_risk_field(wallet, db_path=db_path))


@register_format
class GoAMLFormat(ExportFormat):
    """UNODC goAML suspicious transaction report (JSON projection of goAML 4.x).

    Options: ``rentity_id`` (FIU-assigned reporting entity id, default 0),
    ``currency_code_local`` (ISO 4217, default ``"USD"``) and ``report_code``
    (``"STR"`` or ``"SAR"``, default ``"STR"``).
    """

    name = "goaml"
    schema = {
        "type": "object",
        "required": [
            "rentity_id", "submission_code", "report_code", "submission_date",
            "currency_code_local", "reason", "report_indicators", "activity",
        ],
        "properties": {
            "rentity_id": {"type": "integer", "minimum": 0},
            "submission_code": {"type": "string", "enum": ["E", "M"]},
            "report_code": {"type": "string", "enum": ["STR", "SAR", "CTR", "AIF"]},
            "submission_date": {"type": "string", "pattern": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"},
            "currency_code_local": {"type": "string", "pattern": "[A-Z]{3}"},
            "reason": {"type": "string"},
            "report_indicators": {
                "type": "object",
                "required": ["indicator"],
                "properties": {"indicator": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
            },
            "activity": {
                "type": "object",
                "required": ["report_parties"],
                "properties": {
                    "report_parties": {
                        "type": "object",
                        "required": ["report_party"],
                        "properties": {
                            "report_party": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "required": ["account", "significance", "reason"],
                                    "properties": {
                                        "account": {
                                            "type": "object",
                                            "required": ["account"],
                                            "properties": {"account": {"type": "string"}},
                                        },
                                        "significance": {"type": "integer", "minimum": 0, "maximum": 10},
                                        "reason": {"type": "string"},
                                        "comments": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }

    def build(self, wallet: str, db_path: str | None = None, **options) -> dict:
        risk = build_ivms_risk_field(wallet, db_path=db_path)
        indicators = risk.alert_types or [f"LEDGERLENS_{risk.risk_level}"]
        reason = (
            f"LedgerLens risk score {risk.ledgerlens_score:.0f}/100 ({risk.risk_level}) "
            f"at {risk.score_timestamp}; alerts: {', '.join(risk.alert_types) or 'none'}"
        )
        return {
            "rentity_id": int(options.get("rentity_id", 0)),
            "submission_code": "E",
            "report_code": options.get("report_code", "STR"),
            "submission_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "currency_code_local": options.get("currency_code_local", "USD"),
            "reason": reason,
            "report_indicators": {"indicator": indicators},
            "activity": {
                "report_parties": {
                    "report_party": [
                        {
                            "account": {"account": wallet},
                            "significance": min(10, int(round(risk.ledgerlens_score / 10))),
                            "reason": reason,
                            "comments": f"evidence_hash={risk.evidence_hash}",
                        }
                    ]
                }
            },
        }
