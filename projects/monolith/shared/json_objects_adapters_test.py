"""Caller fence contracts, schema adapters and content-free fact errors."""

import json
import sys
from types import SimpleNamespace

import pytest

from grimoire import fact_extraction as grimoire
from knowledge import extraction as knowledge


@pytest.fixture
def selectors(monkeypatch):
    # Inspect selection independently of the two intentionally different schemas.
    monkeypatch.setattr(
        grimoire,
        "_Extraction",
        SimpleNamespace(model_validate=lambda value: SimpleNamespace(facts=value)),
    )
    monkeypatch.setattr(
        knowledge,
        "_ExtractionResult",
        SimpleNamespace(model_validate=lambda value: value),
    )
    return grimoire.parse_facts, knowledge._parse_result


@pytest.mark.parametrize(
    "text, grimoire_value, knowledge_value",
    [
        ('before ```json {"a":1}``` after {"a":2}', {"a": 1}, {"a": 2}),
        ('```json\n{"a":1}\n```\n{"a":2}', {"a": 1}, {"a": 1}),
        ('```python\n{"a":1}\n```\n{"a":2}', {"a": 2}, {"a": 1}),
        ('```JSON\n{"a":1}\n```\n{"a":2}', {"a": 1}, {"a": 1}),
        ('```json\r\n{"a":1}\r\n```\r\n{"a":2}', {"a": 1}, {"a": 2}),
        ('```json\r\n{"a":1}\r\n```\n{"a":2}', {"a": 1}, {"a": 1}),
        ('```json\n{"a":1}\n```\n```json\ninvalid\n```', {"a": 1}, {"a": 1}),
        ('```json\n{}\n```\n```json\n[{"a":2}]\n```', {}, {}),
        ('```json\n[{"a":2}]\n```', {"a": 2}, {"a": 2}),
        ('{"a":{"b":1}}', {"a": {"b": 1}}, {"a": {"b": 1}}),
        (
            json.dumps({"a": 'escaped " \\ { }'}),
            {"a": 'escaped " \\ { }'},
            {"a": 'escaped " \\ { }'},
        ),
        ('{"a":1} {invalid} {"a":2} {bad}', {"a": 2}, {"a": 2}),
        ("{}", {}, {}),
        ('[{"a":1},{"a":2}]', {"a": 2}, {"a": 2}),
        ('{"a":1} {"truncated":', {"a": 1}, {"a": 1}),
        ('{"a":1} "unmatched {"a":2}', {"a": 1}, {"a": 1}),
    ],
)
def test_caller_selection(selectors, text, grimoire_value, knowledge_value):
    assert selectors[0](text) == grimoire_value
    assert selectors[1](text) == knowledge_value


@pytest.mark.parametrize(
    "text", ["no object", '{"truncated":{"a":1}', '"unmatched {"a":1}']
)
def test_missing_object_adapters(text):
    for parse, error in (
        (grimoire.parse_facts, grimoire.FactExtractionInvalid),
        (knowledge._parse_result, knowledge.ExtractionOutputInvalid),
    ):
        with pytest.raises(error, match="^missing valid JSON object$"):
            parse(text)


def test_caller_integer_limit_order(selectors):
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        text = '{"a":' + "1" * 641 + '} {"a":2}'
        with pytest.raises(ValueError, match="Exceeds the limit"):
            selectors[0](text)
        assert selectors[1](text) == {"a": 2}
    finally:
        sys.set_int_max_str_digits(previous)


def test_real_fact_schema():
    fact = dict(
        statement="Synthetic fact",
        evidence_event_ids=["event"],
        entity_id=None,
        confidence=0.8,
    )
    assert (
        grimoire.parse_facts(json.dumps({"facts": [fact]}))[0].statement
        == fact["statement"]
    )
    assert grimoire.parse_facts('{"facts": []}') == []


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": "0.8"},
        {"confidence": 2},
        {"confidence": float("nan")},
        {"statement": 123},
        {"statement": ""},
        {"evidence_event_ids": []},
        {"entity_id": 123},
        {"extra": "synthetic-private-extra"},
    ],
)
def test_invalid_private_facts_have_content_free_errors(change):
    private = "synthetic-private-statement"
    fact = dict(
        statement=private,
        evidence_event_ids=["synthetic-private-event"],
        entity_id=None,
        confidence=0.8,
    )
    fact.update(change)
    with pytest.raises(grimoire.FactExtractionInvalid) as caught:
        grimoire.parse_facts(json.dumps({"facts": [fact]}))
    assert str(caught.value) == "invalid fact result shape or confidence"
    assert "synthetic-private" not in str(caught.value)


def test_real_knowledge_normalization():
    value = {
        "assertions": [
            {
                "title": "  Synthetic title  ",
                "body": "  " + "x" * 21000 + "  ",
                "scope": "repo:synthetic/example",
                "verification_state": "unverified",
                "confidence": "9",
                "edges": {"subjects": ["subject"] * 25},
                "unresolved_subject": "u" * 250,
            }
        ],
        "unknown": True,
    }
    result = knowledge._parse_result(json.dumps(value)).assertions[0]
    assert result.title == "Synthetic title"
    assert result.body == "x" * 20000
    assert result.confidence == 1.0
    assert result.edges.subjects == ["subject"] * 20
    assert result.unresolved_subject == "u" * 200
