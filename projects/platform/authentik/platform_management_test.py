"""Keep management identity contracts while the retired signup flow stays absent."""

import os
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

CHART = Path(__file__).resolve().parent


class KeyReference(str):
    pass


class Loader(yaml.BaseLoader):
    pass


Loader.add_constructor(
    "!KeyOf", lambda loader, node: KeyReference(loader.construct_scalar(node))
)

# Literal contracts, independent of the production mapping sources. The stable
# platform-profile identity predates retirement of the optional flow (#7075).
GRIMOIRE_MAPPING = {
    "model": "authentik_providers_oauth2.scopemapping",
    "id": "platform-profile",
    "identifiers": {"name": "platform-profile"},
    "attrs": {
        "scope_name": "profile",
        "expression": 'return {"preferred_username": request.user.username, "platform_user_type": request.user.type,\n'
        '    "groups": list(request.user.ak_groups.values_list("name", flat=True))}\n',
    },
}
MCP_MAPPING = {
    "model": "authentik_providers_oauth2.scopemapping",
    "id": "platform-management-profile",
    "identifiers": {"name": "platform-management-profile"},
    "attrs": {
        "scope_name": "profile",
        "expression": 'return {"platform_user_type": request.user.type}\n',
    },
}


def render(management, link, hub, *, retired_flag=False):
    command = [os.environ.get("HELM_BIN", "helm"), "template", "authentik", str(CHART)]
    if hub:
        command += ["-f", str(CHART / "values-gke.yaml")]
    command += [
        "--set",
        f"platformManagement.enabled={str(management).lower()}",
        "--set",
        f"grimoireLinkEnrollment.enabled={str(link).lower()}",
    ]
    if retired_flag:
        # Regression: obsolete values cannot activate a flow or credential guard.
        command += ["--set", "platformEnrollment.enabled=true"]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    documents = list(yaml.safe_load_all(result.stdout))
    data = next(
        document["data"]
        for document in documents
        if document
        and document["kind"] == "ConfigMap"
        and document["metadata"]["name"] == "authentik-mcp-blueprints"
    )
    assert "PLATFORM_AUTH_API_URL" not in result.stdout
    assert "PLATFORM_AUTH_ENROLLMENT_ISSUER" not in result.stdout
    return data


def references(value):
    if isinstance(value, KeyReference):
        return {str(value)}
    if isinstance(value, dict):
        return set().union(*(references(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(references(item) for item in value))
    return set()


@pytest.mark.parametrize("management", [False, True])
@pytest.mark.parametrize("link", [False, True])
@pytest.mark.parametrize("hub", [False, True])
def test_management_render_contract_and_keyof_creation_order(management, link, hub):
    data = render(management, link, hub)
    assert "platform-enrollment.yaml" not in data
    expected_names = {path.name for path in (CHART / "blueprints").glob("*.yaml")}
    if link:
        expected_names.add("grimoire-link-enrollment.yaml")
    assert set(data) == expected_names
    for name, content in data.items():
        parsed = yaml.load(content, Loader=Loader)
        created = set()
        for entry in parsed["entries"]:
            assert references(entry) <= created, (
                name,
                entry.get("id"),
                references(entry) - created,
            )
            if entry.get("id"):
                created.add(entry["id"])

        directory = "optional" if name == "grimoire-link-enrollment.yaml" else "blueprints"
        expected = yaml.load((CHART / directory / name).read_text(), Loader=Loader)
        mapping = {
            "grimoire-auth.yaml": GRIMOIRE_MAPPING,
            "mcp-auth.yaml": MCP_MAPPING,
        }.get(name)
        if management and mapping:
            expected["entries"].insert(0, mapping)
            provider = next(
                entry
                for entry in expected["entries"]
                if entry["model"] == "authentik_providers_oauth2.oauth2provider"
                and entry.get("identifiers", {}).get("name")
                == ("grimoire" if name == "grimoire-auth.yaml" else "mcp-friends")
            )
            provider["attrs"]["property_mappings"].append(KeyReference(mapping["id"]))
        # Every ordinary flow, provider, group, binding and entitlement must stay
        # as authored, with only the two explicit management mappings inserted.
        assert parsed == expected, name

    grimoire = yaml.load(data["grimoire-auth.yaml"], Loader=Loader)
    gate = next(entry for entry in grimoire["entries"] if entry.get("id") == "human-access")
    assert gate["attrs"]["expression"] == (
        'return request.user.is_active and request.user.type != "service_account" and bool(request.user.email)\n'
    )
    assert render(management, link, hub, retired_flag=True) == data


def test_retired_configuration_and_sources_are_absent():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert "platformEnrollment" not in values
    assert values["platformManagement"]["enabled"] is True
    assert values["grimoireLinkEnrollment"]["enabled"] is False
    names = {variable["name"] for variable in values["authentik"]["global"]["env"]}
    assert names.isdisjoint({"PLATFORM_AUTH_API_URL", "PLATFORM_AUTH_ENROLLMENT_ISSUER"})
    assert not (CHART / "optional/platform-enrollment.yaml").exists()
    assert not (CHART / "optional/platform-profile.yaml").exists()
    template = (CHART / "templates/blueprints-configmap.yaml").read_text()
    for retired in ("platformEnrollment", "platform-enrollment", "PLATFORM_AUTH_"):
        assert retired not in template


@pytest.mark.parametrize("user_type", ["internal", "external", "service_account"])
def test_management_claim_values(user_type):
    data = render(True, False, False)

    def groups(field, *, flat):
        assert (field, flat) == ("name", True)
        return ["operators", "family"]

    request = SimpleNamespace(
        user=SimpleNamespace(
            username="operator-fixture",
            type=user_type,
            ak_groups=SimpleNamespace(values_list=groups),
        )
    )
    for name, identity, expected in (
        (
            "grimoire-auth.yaml",
            "platform-profile",
            {
                "preferred_username": "operator-fixture",
                "platform_user_type": user_type,
                "groups": ["operators", "family"],
            },
        ),
        ("mcp-auth.yaml", "platform-management-profile", {"platform_user_type": user_type}),
    ):
        mapping = next(
            entry
            for entry in yaml.load(data[name], Loader=Loader)["entries"]
            if entry.get("id") == identity
        )
        context = {}
        source = "def claims(request):\n" + textwrap.indent(mapping["attrs"]["expression"], "    ")
        exec(source, context)  # noqa: S102 - Trusted checked-in mapping under test.
        assert context["claims"](request) == expected
