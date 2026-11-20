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


def _entries(filename):
    path = Path(__file__).parent / "factory-blueprints" / filename
    return yaml.load(path.read_text(), Loader=BlueprintLoader)["entries"]


def test_bosun_groups():
    groups = {
        entry["identifiers"]["name"]: entry["attrs"]
        for entry in _entries("bosun-apply.yaml")
        if entry["model"] == "authentik_core.group"
    }
    for name in ("bosun-member", "discord-operators", "bosun-applicants"):
        assert groups[name]["is_superuser"] is False
        assert groups[name]["roles"] == []
    assert (
        groups["bosun-member"]["parents"] == groups["bosun-applicants"]["parents"] == []
    )
    assert [p.value for p in groups["discord-operators"]["parents"]] == ["member"]
    assert set(groups["authentik Admins"]) == {"parents"}
    assert [p.value for p in groups["authentik Admins"]["parents"]] == ["operators"]


def test_bosun_apply_discord():
    entries = {e.get("id"): e for e in _entries("bosun-apply.yaml")}
    assert entries["apply"]["identifiers"]["slug"] == "bosun-apply"
    assert entries["start"]["identifiers"]["target"].value == "apply"
    assert entries["start"]["identifiers"]["stage"].value == "discord-redirect"
    assert (
        entries["discord-redirect"]["attrs"]["target_static"]
        == "/source/oauth/login/discord/"
    )
    social = {e.get("id"): e for e in _entries("factory-social.yaml")}
    expression = social["record-application"]["attrs"]["expression"]
    assert 'request.context["source"].slug == "discord"' in expression
    assert 'user.groups.add(Group.objects.get(name="bosun-applicants"))' in expression


def test_factory_operator_apps_keep_operator_group_bindings():
    expected = {"factory-edge", "factory-argocd", "factory-itsaplan", "factory-grafana"}
    seen = set()
    for param in _applications():
        app, entries = param.values
        slug = app["identifiers"]["slug"]
        if slug in expected:
            seen.add(slug)
            names = []
            for _, binding in _binding_entries_for(app, entries):
                group = binding["group"]
                assert group.tag == "!Find"
                model, lookup = group.value
                assert model.value == "authentik_core.group"
                field, name = lookup.value
                assert field.value == "name"
                assert binding.get("enabled", True) and not binding.get("negate")
                names.append(name.value)
            assert sorted(names) == ["authentik Admins", "factory-operators"]
    assert seen == expected


def test_abandoned_apply_preserves_discord_application_login(monkeypatch):
    import sys
    from textwrap import indent
    from time import time
    from types import SimpleNamespace
    from unittest.mock import Mock

    app_return = "/application/o/authorize/?client_id=factory-edge"
    user = Mock()
    user.groups.exists.return_value = False
    session = {"bosun_apply_until": time() + 1800}
    plan = SimpleNamespace(context={"redirect": app_return})
    request = SimpleNamespace(
        context={
            "flow_plan": plan,
            **plan.context,
            "source": SimpleNamespace(slug="discord"),
            "pending_user": user,
        },
        http_request=SimpleNamespace(session=session),
    )
    monkeypatch.setitem(
        sys.modules, "authentik.core.models", SimpleNamespace(Group=Mock())
    )
    monkeypatch.setitem(
        sys.modules,
        "authentik.stages.authenticator",
        SimpleNamespace(devices_for_user=lambda user: []),
    )
    social = {e.get("id"): e for e in _entries("factory-social.yaml")}

    def evaluate(policy):
        scope = {"request": request, "ak_message": Mock()}
        exec(
            "def run():\n" + indent(social[policy]["attrs"]["expression"], "    "),
            scope,
        )
        return scope["run"]()

    assert not evaluate("social-session-ready")
    user.groups.exists.return_value = True
    assert evaluate("social-session-ready")
    assert evaluate("record-application")
    assert plan.context["redirect"] == app_return
    assert "bosun_apply_until" not in session
    user.groups.add.assert_not_called()
