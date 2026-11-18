"""Every application on the open-signup factory instance needs a group gate."""

from pathlib import Path

import pytest
import yaml


class BlueprintLoader(yaml.SafeLoader):
    """Keep Authentik references as tagged nodes without evaluating them."""


BlueprintLoader.add_multi_constructor("!", lambda loader, tag, node: node)


def _applications():
    paths = sorted((Path(__file__).parent / "factory-blueprints").glob("*.yaml"))
    assert paths, "No factory blueprints discovered"
    for path in paths:
        entries = yaml.load(path.read_text(), Loader=BlueprintLoader)["entries"]
        for app in entries:
            if app["model"] == "authentik_core.application":
                yield pytest.param(
                    app, entries, id=f"{path.name}-{app['identifiers']['slug']}"
                )


def _binding_entries_for(app, entries):
    for entry in entries:
        if entry["model"] != "authentik_policies.policybinding":
            continue
        fields = {**entry.get("identifiers", {}), **entry.get("attrs", {})}
        target = fields.get("target")
        if (
            entry.get("state", "present") == "present"
            and isinstance(target, yaml.ScalarNode)
            and target.tag == "!KeyOf"
            and target.value == app.get("id")
        ):
            yield entry, fields


def _assert_group_binding(app, bindings):
    assert any(
        fields.get("group")
        and fields.get("enabled", True) is True
        and fields.get("negate", False) is False
        for fields in bindings
    ), f"Missing enabled group binding for {app['identifiers']['slug']}"


@pytest.mark.parametrize("app,entries", _applications())
def test_factory_application_has_group_binding(app, entries):
    bindings = (fields for _, fields in _binding_entries_for(app, entries))
    _assert_group_binding(app, bindings)


@pytest.mark.parametrize("app,entries", _applications())
def test_removing_group_bindings_denies_application(app, entries):
    removed = [
        entry
        for entry, fields in _binding_entries_for(app, entries)
        if "group" in fields
    ]
    assert removed
    mutated_entries = [entry for entry in entries if entry not in removed]
    with pytest.raises(
        AssertionError,
        match=f"Missing enabled group binding for {app['identifiers']['slug']}",
    ):
        test_factory_application_has_group_binding(app, mutated_entries)


@pytest.mark.parametrize(
    "fields", [{"negate": True}, {"enabled": False}, {"policy": "p"}, {"user": "u"}]
)
def test_rejects_ineffective_bindings(fields):
    if "negate" in fields or "enabled" in fields:
        fields = {**fields, "group": "operators"}
    with pytest.raises(AssertionError, match="Missing enabled group binding"):
        _assert_group_binding({"identifiers": {"slug": "example"}}, [fields])
