"""The optional Discord source is off by default and touches only Grimoire."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

CHART = Path(__file__).resolve().parent
MARKER = "      # DISCORD_AUTHENTICATION_FLOW"


class KeyReference(str):
    pass


class Loader(yaml.BaseLoader):
    pass


Loader.add_constructor(
    "!KeyOf", lambda loader, node: KeyReference(loader.construct_scalar(node))
)


def render(enabled, *, management=True, hub=False):
    command = [os.environ.get("HELM_BIN", "helm"), "template", "authentik", str(CHART)]
    if hub:
        command += ["-f", str(CHART / "values-gke.yaml")]
    command += [
        "--set",
        f"grimoireDiscordSource.enabled={str(enabled).lower()}",
        "--set",
        f"platformManagement.enabled={str(management).lower()}",
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    documents = [d for d in yaml.safe_load_all(result.stdout) if d]
    blueprints = next(
        d["data"]
        for d in documents
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"] == "authentik-mcp-blueprints"
    )
    items = {d["metadata"]["name"] for d in documents if d["kind"] == "OnePasswordItem"}
    return blueprints, items


def fragment():
    return yaml.load(
        (CHART / "optional/grimoire-discord-source.yaml").read_text(), Loader=Loader
    )


def references(value):
    if isinstance(value, KeyReference):
        return {str(value)}
    if isinstance(value, dict):
        return set().union(*(references(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(references(v) for v in value))
    return set()


def test_disabled_by_default_and_secret_references_are_optional():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["grimoireDiscordSource"]["enabled"] is False
    env = {v["name"]: v for v in values["authentik"]["global"]["env"]}
    for name in ("GRIMOIRE_DISCORD_CLIENT_ID", "GRIMOIRE_DISCORD_CLIENT_SECRET"):
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "grimoire-discord-oauth" and ref["optional"] is True
    blueprints, items = render(False)
    assert "grimoire-discord-oauth" not in items
    assert MARKER in blueprints["grimoire-auth.yaml"]
    assert not any("oauthsource" in content for content in blueprints.values())


@pytest.mark.parametrize("management", [False, True])
@pytest.mark.parametrize("hub", [False, True])
def test_enabled_changes_only_grimoire_and_resolves_in_creation_order(management, hub):
    before, _ = render(False, management=management, hub=hub)
    after, items = render(True, management=management, hub=hub)
    assert "grimoire-discord-oauth" in items
    assert set(after) == set(before)
    assert {k for k in after if after[k] != before[k]} == {"grimoire-auth.yaml"}

    expected = yaml.load(before["grimoire-auth.yaml"], Loader=Loader)
    expected["entries"][:0] = fragment()
    provider = next(
        e
        for e in expected["entries"]
        if e.get("identifiers", {}).get("name") == "grimoire"
    )
    provider["attrs"]["authentication_flow"] = KeyReference("discord-authentication")
    parsed = yaml.load(after["grimoire-auth.yaml"], Loader=Loader)
    assert parsed == expected
    assert ("platform-profile" in references(provider)) is management

    created = set()
    for entry in parsed["entries"]:
        assert references(entry) <= created, (
            entry.get("id"),
            references(entry) - created,
        )
        if entry.get("id"):
            created.add(entry["id"])


def test_grimoire_login_retains_password_mfa_and_login_stages():
    entries = fragment()
    identification = next(e for e in entries if e.get("id") == "discord-identification")
    attrs = identification["attrs"]
    assert set(attrs["user_fields"]) == {"username", "email"}
    assert attrs["password_stage"] == [
        "authentik_stages_password.passwordstage",
        ["name", "default-authentication-password"],
    ]
    assert attrs["sources"] == ["discord-source"]
    bindings = [
        e["identifiers"]
        for e in entries
        if e["model"] == "authentik_flows.flowstagebinding"
        and e["identifiers"]["target"] == "discord-authentication"
    ]
    assert [(b["order"], b["stage"]) for b in bindings] == [
        ("10", "discord-identification"),
        (
            "30",
            [
                "authentik_stages_authenticator_validate.authenticatorvalidatestage",
                ["name", "default-authentication-mfa-validation"],
            ],
        ),
        (
            "100",
            [
                "authentik_stages_user_login.userloginstage",
                ["name", "default-authentication-login"],
            ],
        ),
    ]


def test_fragment_provisions_no_access():
    entries = fragment()
    assert not any(
        part in e["model"]
        for e in entries
        for part in ("rbac", "token", "group", "oauth2provider", "application")
    )
    source = next(e for e in entries if e["model"].endswith(".oauthsource"))["attrs"]
    assert source["provider_type"] == "discord"
    assert source["user_matching_mode"] == "identifier"
    assert source["user_path_template"] == "grimoire"
    assert source["enrollment_flow"] == "discord-enrollment"
    write = next(e for e in entries if e["model"].endswith("userwritestage"))["attrs"]
    assert write["user_creation_mode"] == "always_create"
    assert write["user_type"] == "external"
    assert write["create_users_group"] == "null"
    assert write["user_path_template"] == "grimoire"
    gate = next(e for e in entries if e["model"] == "authentik_policies.policybinding")
    assert gate["identifiers"]["target"] == "discord-enrollment"
    assert gate["identifiers"]["policy"] == "discord-enrollment-gate"


def expression(model_suffix):
    return next(e for e in fragment() if e["model"].endswith(model_suffix))["attrs"][
        "expression"
    ]


@pytest.fixture
def run_gate(monkeypatch):
    existing = SimpleNamespace(value=False)
    for name in ("authentik", "authentik.core", "authentik.core.models"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["authentik.core.models"].User = SimpleNamespace(
        objects=SimpleNamespace(
            filter=lambda **kwargs: SimpleNamespace(exists=lambda: existing.value)
        )
    )
    source = "def policy():\n" + textwrap.indent(expression("expressionpolicy"), "    ")

    def run(info):
        context = {
            "request": SimpleNamespace(context={"oauth_userinfo": info}),
            "ak_message": lambda message: None,
        }
        exec(source, context)  # noqa: S102 - reviewed local blueprint policy under test
        return context["policy"]()

    return run, existing


VERIFIED = {
    "id": "1234",
    "username": "rowan",
    "email": "Rowan@example.test",
    "verified": True,
}


def test_gate_accepts_verified_discord_identity(run_gate):
    run, _ = run_gate
    assert run(VERIFIED) is True


@pytest.mark.parametrize(
    "info",
    [
        {},
        {**VERIFIED, "id": ""},
        {**VERIFIED, "verified": False},
        {**VERIFIED, "verified": "true"},
        {**VERIFIED, "email": None},
    ],
)
def test_gate_rejects_missing_or_unverified_identity(run_gate, info):
    run, _ = run_gate
    assert run(info) is False


def test_gate_never_claims_an_existing_email(run_gate):
    run, existing = run_gate
    existing.value = True
    assert run(VERIFIED) is False


def mapping(info):
    context = {"info": info}
    exec(  # noqa: S102 - reviewed local blueprint mapping under test
        "def props():\n"
        + textwrap.indent(expression("oauthsourcepropertymapping"), "    "),
        context,
    )
    return context["props"]()


def test_mapping_uses_stable_username_and_verified_email_only():
    assert mapping({**VERIFIED, "global_name": "Rowan"}) == {
        "username": "discord-1234",
        "name": "Rowan",
        "email": "Rowan@example.test",
    }
    unverified = mapping({**VERIFIED, "verified": False})
    assert unverified["username"] == "discord-1234"
    assert unverified["name"] == "rowan"
    assert unverified["email"] is None
