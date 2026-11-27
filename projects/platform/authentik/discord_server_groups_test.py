"""Discord sign-in syncs one group per allowlisted Discord server (HOME-917).

The bridge allowlist, the Authentik groups and the edge's artifact folders
must name the same servers.
"""

import os
from pathlib import Path
from textwrap import indent
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEPLOY_VALUES = os.environ.get(
    "DEPLOY_VALUES", ROOT / "projects/factory/deploy/values.yaml"
)
EDGE_VALUES = os.environ.get(
    "EDGE_VALUES", ROOT / "projects/platform-factory/edge/chart/values.yaml"
)


class BlueprintLoader(yaml.SafeLoader):
    """Keep Authentik references as tagged nodes without evaluating them."""


BlueprintLoader.add_multi_constructor("!", lambda loader, tag, node: node)


def entries(name):
    path = HERE / "factory-blueprints" / name
    return yaml.load(path.read_text(), Loader=BlueprintLoader)["entries"]


def by_id(name):
    return {e["id"]: e for e in entries(name) if "id" in e}


def server_groups():
    return {
        e["attrs"]["attributes"]["discord_server_id"]: e["identifiers"]["name"]
        for e in entries("factory-auth.yaml")
        if e["model"] == "authentik_core.group"
        and "discord_server_id" in e["attrs"].get("attributes", {})
    }


def run(expression, **scope):
    exec("def evaluate():\n" + indent(expression, "    "), scope)
    return scope["evaluate"]()


@pytest.mark.skipif(
    not Path(DEPLOY_VALUES).exists(),
    reason="only :discord_server_groups_test has the other packages' values",
)
def test_groups_match_the_bridge_allowlist_and_the_edge_folders():
    deploy = yaml.safe_load(Path(DEPLOY_VALUES).read_text())
    bridge = set(deploy["discord"]["bridge"]["policy"]["guilds"])
    edge = yaml.safe_load(Path(EDGE_VALUES).read_text())
    groups = server_groups()
    assert set(groups) == bridge
    assert set(edge["artifacts"]["servers"]) == bridge
    assert all(name == "discord-" + server for server, name in groups.items())


def test_discord_sign_in_maps_only_allowlisted_servers():
    social = by_id("factory-social.yaml")
    source = social["discord"]["attrs"]
    assert source["additional_scopes"] == "guilds"
    assert source["group_matching_mode"] == "name_link"
    assert [m.value for m in source["user_property_mappings"]] == ["discord-properties"]
    for other in ("github", "google"):
        assert "additional_scopes" not in social[other]["attrs"]
        assert "group_matching_mode" not in social[other]["attrs"]

    groups = server_groups()
    marked = [
        SimpleNamespace(name=name, attributes={"discord_server_id": server})
        for server, name in groups.items()
    ]
    models = SimpleNamespace(Group=Mock())
    models.Group.objects.filter.return_value = marked
    expression = social["discord-properties"]["attrs"]["expression"]
    homelab, loom = "1501965852042330302", "1512814732392927463"

    def sign_in(status, guilds):
        client = Mock()
        client.session.get.return_value = SimpleNamespace(
            status_code=status, json=lambda: [{"id": g} for g in guilds]
        )
        with patch.dict("sys.modules", {"authentik.core.models": models}):
            out = run(
                expression,
                info={"id": 7},
                client=client,
                token={"access_token": "secret", "token_type": "Bearer"},
            )
        url = client.session.get.call_args.args[0]
        headers = client.session.get.call_args.kwargs["headers"]
        # The token goes in a header, never in the URL.
        assert url == "https://discord.com/api/users/@me/guilds"
        assert headers == {"Authorization": "Bearer secret"}
        return out

    out = sign_in(200, [loom, "999999999999999999", homelab])
    assert out == {
        "username": "discord-7",
        "groups": ["discord-" + homelab, "discord-" + loom],
    }
    models.Group.objects.filter.assert_called_with(
        attributes__has_key="discord_server_id"
    )
    assert sign_in(200, []) == {"username": "discord-7", "groups": []}
    # A failed lookup fails closed: no server groups until the next sign-in.
    assert sign_in(429, [homelab]) == {"username": "discord-7", "groups": []}


def test_a_synced_member_passes_the_session_gate_and_the_session_is_bounded():
    social = by_id("factory-social.yaml")
    expression = social["social-session-ready"]["attrs"]["expression"]
    user = Mock()
    user.groups.exists.return_value = False

    def ready(source_groups):
        request = SimpleNamespace(
            context={
                "pending_user": user,
                "source": SimpleNamespace(slug="discord"),
                "redirect": "/application/o/authorize/",
                "source_groups": source_groups,
            },
            http_request=SimpleNamespace(session={}),
        )
        auth = SimpleNamespace(devices_for_user=lambda user: [])
        with patch.dict("sys.modules", {"authentik.stages.authenticator": auth}):
            return run(expression, request=request, ak_message=Mock())

    assert not ready({})
    assert ready({"discord-1501965852042330302": {"name": "x"}})
    assert social["social-login"]["attrs"]["session_duration"] == "hours=24"


def test_edge_token_carries_only_the_groups_the_edge_checks():
    auth = by_id("factory-auth.yaml")
    provider = auth["edge-provider"]["attrs"]
    assert provider["access_token_validity"] == "minutes=10"
    names = [
        m.value if isinstance(m, yaml.ScalarNode) else m.value[1].value[0].value
        for m in provider["property_mappings"]
    ]
    assert "edge-profile" in names
    assert not any(
        isinstance(m, yaml.SequenceNode) and "scope-profile" in str(m.value)
        for m in provider["property_mappings"]
    )
    mapping = auth["edge-profile"]["attrs"]
    assert mapping["scope_name"] == "profile"

    def group(name, **attributes):
        return SimpleNamespace(name=name, attributes=attributes)

    user = SimpleNamespace(
        name="Friend",
        username="discord-7",
        groups=Mock(),
    )
    user.groups.all.return_value = [
        group("authentik Admins"),
        group("factory-kube-viewers"),
        group("discord-operators"),
        group("discord-1501965852042330302", discord_server_id="1501965852042330302"),
    ]
    claims = run(mapping["expression"], request=SimpleNamespace(user=user))
    assert claims["groups"] == ["authentik Admins", "discord-1501965852042330302"]
    assert claims["preferred_username"] == "discord-7"
