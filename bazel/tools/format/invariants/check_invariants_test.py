import pytest
import yaml

from check_invariants import InvariantLoader, eligible_tests, validate

ROW = dict(
    id="DEMO-1",
    statement="Something holds.",
    mechanism=["foo/bar"],
    state="live",
    test="//foo:test",
)


def test_states():
    live = validate(
        {
            "scope": {
                "invariants": [
                    ROW,
                    dict(
                        id="DEMO-2",
                        statement="Designed.",
                        mechanism=[],
                        state="designed",
                    ),
                    dict(
                        id="DEMO-3",
                        statement="Unproven.",
                        mechanism=["foo"],
                        state="unproven",
                    ),
                ]
            }
        }
    )
    assert live == {"//foo:test": ["scope: DEMO-1"]}


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad"},
        {"statement": " "},
        {"state": "unknown"},
        {"mechanism": "foo"},
        {"mechanism": ["/absolute"]},
        {"mechanism": ["../foo"]},
        {"mechanism": []},
        {"state": "designed"},
        {"test": None},
        {"test": "foo"},
        {"extra": None},
    ],
)
def test_bad_row(changes):
    with pytest.raises(ValueError):
        validate({"scope": {"invariants": [ROW | changes]}})


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {"invariants": {}},
        {"invariants": [{}]},
        {"invariants": [{k: v for k, v in ROW.items() if k != "test"}]},
    ],
)
def test_bad_shape_or_missing_field(document):
    with pytest.raises(ValueError):
        validate({"scope": document})


def test_global_duplicate():
    with pytest.raises(ValueError, match="duplicate"):
        validate({"a": {"invariants": [ROW]}, "b": {"invariants": [ROW]}})


@pytest.mark.parametrize(
    "rule, expected",
    [
        ("", False),
        ('<rule class="go_library" name="//foo:test"/>', False),
        (
            '<rule class="go_test" name="//foo:test"><list name="tags"><string value="manual"/></list></rule>',
            False,
        ),
        (
            '<rule class="py_test" name="//foo:test"><list name="tags"><string value="manual-review"/></list></rule>',
            True,
        ),
    ],
)
def test_live_test_eligibility(rule, expected):
    assert ("//foo:test" in eligible_tests("<query>" + rule + "</query>")) == expected


def test_duplicate_yaml_field():
    with pytest.raises(yaml.YAMLError, match="duplicate mapping key"):
        yaml.load("state: live\nstate: designed", Loader=InvariantLoader)
