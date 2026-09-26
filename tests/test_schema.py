"""Tests for the triage decision schema (Epic 1, story 1)."""

import json

import pytest
from pydantic import ValidationError

from triage.schema import TriageDecision, TriageValidationError, validate_decision

VALID = {
    "category": "billing",
    "priority": "P2",
    "route": "billing-team",
    "rationale": "Double charge puts money at stake.",
}


def rejected(payload) -> str:
    with pytest.raises(TriageValidationError) as error:
        validate_decision(payload)
    return str(error.value)


def test_valid_dict():
    decision = validate_decision(VALID)
    assert isinstance(decision, TriageDecision)
    assert decision.model_dump() == VALID


def test_valid_json_text():
    assert validate_decision(json.dumps(VALID)) == validate_decision(VALID)
    assert validate_decision(json.dumps(VALID).encode()) == validate_decision(VALID)


@pytest.mark.parametrize(("field", "value"), [("category", "sales"), ("priority", "P0"), ("route", "sales-team")])
def test_unknown_value(field, value):
    assert field in rejected({**VALID, field: value})


def test_missing_field():
    payload = {k: v for k, v in VALID.items() if k != "route"}
    assert "route" in rejected(payload)


@pytest.mark.parametrize("rationale", ["", "   "])
def test_empty_rationale(rationale):
    assert "rationale" in rejected({**VALID, "rationale": rationale})


def test_extra_field():
    assert "confidence" in rejected({**VALID, "confidence": 0.9})


def test_several_problems_in_one_message():
    payload = {k: v for k, v in VALID.items() if k != "route"} | {"priority": "P0"}
    message = rejected(payload)
    assert "priority" in message and "route" in message


def test_route_and_category_are_independent():
    decision = validate_decision({**VALID, "route": "bug-team"})
    assert (decision.category, decision.route) == ("billing", "bug-team")


@pytest.mark.parametrize(
    "rationale",
    [
        "Money is at stake. Customer is angry.",
        "Money is at stake! Customer is angry.",
        "Is money at stake? Customer is angry.",
        'Customer says "charged twice." Customer is angry.',
        "Money is at stake (double charge.) Customer is angry.",
    ],
)
def test_multi_sentence_rationale(rationale):
    assert "rationale" in rejected({**VALID, "rationale": rationale})


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("not json", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        ('{"n": ' + "1" * 5000 + "}", "not valid JSON"),
        ("[1, 2]", "must be a JSON object, got list"),
        ([1, 2], "must be a JSON object, got list"),
        (None, "must be a JSON object, got NoneType"),
    ],
)
def test_not_json_or_not_an_object(payload, reason):
    assert reason in rejected(payload)


def test_rationale_is_trimmed():
    decision = validate_decision({**VALID, "rationale": "  Money is at stake.\n"})
    assert decision.rationale == "Money is at stake."


def test_error_message_format():
    assert rejected({**VALID, "rationale": "A. B."}) == "Invalid triage decision: rationale: must be a single sentence"


def test_validation_error_is_a_value_error():
    assert issubclass(TriageValidationError, ValueError)


def test_decision_is_immutable():
    decision = validate_decision(VALID)
    with pytest.raises(ValidationError):
        decision.priority = "P0"


def test_json_schema_lists_allowed_values_and_descriptions():
    properties = TriageDecision.model_json_schema()["properties"]
    assert properties["category"]["enum"] == ["billing", "bug", "access", "performance", "how-to"]
    assert properties["priority"]["enum"] == ["P1", "P2", "P3", "P4"]
    assert properties["route"]["enum"] == ["billing-team", "bug-team", "access-team", "performance-team", "how-to-team"]
    assert all(field.get("description") for field in properties.values())


def test_json_schema_forbids_extra_fields_and_requires_all():
    schema = TriageDecision.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["category", "priority", "route", "rationale"]
