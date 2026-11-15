"""Snapshot conformance, independent of the browser's schema interpreter."""

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
SCHEMA = json.loads((ROOT / "schema/v1/snapshot.schema.json").read_text())
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)


def read(path):
    return json.loads((FIXTURES / path).read_text())


def validator(kind):
    return Draft7Validator({**SCHEMA, "oneOf": [{"$ref": f"#/definitions/{kind}"}]})


def test_schema_is_valid():
    Draft7Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("path", sorted(FIXTURES.rglob("*.json")))
def test_every_fixture_validates(path):
    VALIDATOR.validate(json.loads(path.read_text()))


def test_manifest_lists_every_object_with_the_right_identity_and_hash():
    references = [(MANIFEST["story"], "story", None)]
    for kind in ("goal", "issue", "session"):
        references.extend(
            (path, kind, identifier)
            for identifier, path in MANIFEST[f"{kind}s"].items()
        )
    paths = {path for path, _, _ in references}
    assert paths == {
        str(path.relative_to(FIXTURES))
        for path in (FIXTURES / "objects").glob("*.json")
    }
    assert len(paths) == len(references)
    for path, kind, identifier in references:
        raw = (FIXTURES / path).read_bytes()
        assert path == f"objects/{hashlib.sha256(raw).hexdigest()}.json"
        obj = json.loads(raw)
        validator(kind).validate(obj)
        if identifier is not None:
            assert obj["key" if kind == "issue" else "id"] == identifier


def test_typed_cross_links_resolve():
    story = read(MANIFEST["story"])
    for summary in story["goals"]:
        assert summary["goalId"] in MANIFEST["goals"]
    for summary in story["workInProgress"] + story["recentlyShipped"]:
        issue = read(MANIFEST["issues"][summary["issueKey"]])
        assert summary["goalId"] == issue["goalId"]
    for identifier, path in MANIFEST["goals"].items():
        for key in read(path)["issueKeys"]:
            assert read(MANIFEST["issues"][key])["goalId"] == identifier
    for path in MANIFEST["issues"].values():
        issue = read(path)
        assert issue["key"] in read(MANIFEST["goals"][issue["goalId"]])["issueKeys"]
        for event in issue["timeline"]:
            if event["kind"] == "session":
                session = read(MANIFEST["sessions"][event["sessionId"]])
                assert session["issueKey"] == issue["key"]
                assert event["turn"] in {turn["number"] for turn in session["turns"]}
    for path in MANIFEST["sessions"].values():
        session = read(path)
        issue = read(MANIFEST["issues"][session["issueKey"]])
        assert session["goalId"] == issue["goalId"]


def test_fixtures_cover_required_cases():
    issues = [read(path) for path in MANIFEST["issues"].values()]
    assert {issue["state"] for issue in issues} == set(
        SCHEMA["definitions"]["state"]["enum"]
    )
    assert any(not read(path)["issueKeys"] for path in MANIFEST["goals"].values())
    story = read(MANIFEST["story"])
    assert "{{stat:unknown}}" in " ".join(story["narrative"])
    assert "unknown" not in story["stats"]
    turns = read(MANIFEST["sessions"]["demo-build"])["turns"]
    assert len(turns) > 20
    assert any(turn["withheld"] for turn in turns)
    assert any(len(turn.get("toolCalls", [])) > 1 for turn in turns)
    assert any(turn.get("diff") for turn in turns)
    assert any("<script>" in turn.get("text", "") for turn in turns)


@pytest.mark.parametrize(
    "replacement",
    [
        {"version": 2},
        {"url": "https://example.invalid"},
        {"narrative": ["Shipped 42 issues."]},
        {"narrative": ["Shipped {{stat:broken} issues."]},
        {"stats": {"shipped": {"label": "Shipped", "value": "42", "unit": "issues"}}},
        {"stats": {"shipped": {"label": "Shipped", "value": 42, "unit": "invalid"}}},
    ],
)
def test_story_rejects_invalid_fields(replacement):
    assert not validator("story").is_valid({**read(MANIFEST["story"]), **replacement})


@pytest.mark.parametrize(
    "field,value", [("text", "hidden"), ("toolCalls", []), ("diff", "hidden")]
)
def test_withheld_turn_rejects_every_content_field(field, value):
    session = read(MANIFEST["sessions"]["demo-build"])
    hidden = next(turn for turn in session["turns"] if turn["withheld"])
    session["turns"] = [{**hidden, field: value}]
    assert not validator("session").is_valid(session)


@pytest.mark.parametrize(
    "replacement",
    [
        {"story": "../private.json"},
        {"story": "objects/not-a-hash.json"},
        {"goals": {"javascript:alert(1)": MANIFEST["story"]}},
        {"issues": {"../DEMO-104": MANIFEST["story"]}},
    ],
)
def test_manifest_rejects_unsafe_references(replacement):
    assert not validator("manifest").is_valid({**MANIFEST, **replacement})


def test_rejects_invalid_states_rates_and_turn_numbers():
    story = read(MANIFEST["story"])
    story["goals"][0]["state"] = "running"
    assert not validator("story").is_valid(story)
    story = read(MANIFEST["story"])
    story["charts"]["firstPassReviewRate"] = [{"label": "Week", "value": 101}]
    assert not validator("story").is_valid(story)
    session = read(MANIFEST["sessions"]["demo-build"])
    session["turns"][0]["number"] = 9007199254740992
    assert not validator("session").is_valid(session)
    session["turns"] = [{"withheld": False}]
    assert not validator("session").is_valid(session)
