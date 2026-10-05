"""The optional Grimoire flow does not alter active flows or provision access."""

import hashlib
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def blueprint():
    return yaml.load(
        (CHART / "optional/grimoire-link-enrollment.yaml").read_text(),
        Loader=yaml.BaseLoader,
    )


def render(enabled):
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "authentik",
            str(CHART),
            "--set",
            f"grimoireLinkEnrollment.enabled={str(enabled).lower()}",
            # Exercise the legacy flow in isolation; production uses the
            # mutually exclusive platform possession flow.
            "--set",
            "platformEnrollment.enabled=false",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return next(
        d["data"]
        for d in yaml.safe_load_all(result.stdout)
        if d
        and d["kind"] == "ConfigMap"
        and d["metadata"]["name"] == "authentik-mcp-blueprints"
    )


def test_flow_is_disabled_by_default_and_active_blueprints_unchanged():
    assert (
        yaml.safe_load((CHART / "values.yaml").read_text())["grimoireLinkEnrollment"][
            "enabled"
        ]
        is False
    )
    before, after = render(False), render(True)
    assert "grimoire-link-enrollment.yaml" not in before
    assert set(after) - set(before) == {"grimoire-link-enrollment.yaml"}
    assert all(after[key] == value for key, value in before.items())
    entries = blueprint()["entries"]
    assert not any(
        any(
            part in row["model"]
            for part in ("rbac", "token", "group", "oauth2provider", "application")
        )
        for row in entries
    )
    write = next(e for e in entries if e["model"].endswith("userwritestage"))["attrs"]
    assert (
        write["user_creation_mode"] == "always_create"
        and write["user_type"] == "external"
    )
    assert write["create_users_group"] == "null"
    assert not any(e.get("attrs", {}).get("field_key") == "username" for e in entries)


@pytest.fixture
def run_policy(monkeypatch):
    existing = SimpleNamespace(value=False)
    for name in (
        "django",
        "django.utils",
        "django.core",
        "django.core.validators",
        "django.core.exceptions",
        "authentik",
        "authentik.core",
        "authentik.core.models",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["django.utils"].timezone = SimpleNamespace(
        now=lambda: datetime.now(timezone.utc)
    )
    sys.modules["django.core.exceptions"].ValidationError = ValueError

    def validate(value):
        if "@" not in value:
            raise ValueError()

    sys.modules["django.core.validators"].validate_email = validate
    sys.modules["authentik.core.models"].User = SimpleNamespace(
        objects=SimpleNamespace(
            filter=lambda **kwargs: SimpleNamespace(exists=lambda: existing.value)
        )
    )
    expression = next(
        e for e in blueprint()["entries"] if e["model"].endswith("expressionpolicy")
    )["attrs"]["expression"]

    def run(**changes):
        email = changes.get("email", "new@example.test")
        invitation = SimpleNamespace(
            single_use=changes.get("single_use", True),
            flow=SimpleNamespace(slug=changes.get("flow", "grimoire-link-enrollment")),
            expires=changes.get(
                "expires", datetime.now(timezone.utc) + timedelta(days=1)
            ),
            fixed_data={
                "email": email,
                "username": changes.get(
                    "username", "grimoire-" + hashlib.sha256(email.encode()).hexdigest()
                ),
            },
        )
        data = {
            "name": "Player",
            "password": changes.get("password", "test-password-only"),
            "password_repeat": "test-password-only",
            "email": "attacker@example.test",
            "username": "admin",
            "is_active": True,
            "type": "internal",
            "attributes": {"privilege": "bad"},
        }
        plan = SimpleNamespace(context={"invitation": invitation, "groups": ["bad"]})
        context = {
            "request": SimpleNamespace(
                context={"flow_plan": plan, "prompt_data": data}
            ),
            "ak_message": lambda message: None,
        }
        exec("def policy():\n" + textwrap.indent(expression, "    "), context)  # noqa: S102 - reviewed local blueprint policy under test
        return context["policy"](), data, plan.context

    return run, existing


def test_policy_allowlists_fields_and_pins_identity(run_policy):
    run, _ = run_policy
    accepted, data, context = run()
    assert accepted
    assert set(data) == {"name", "password", "password_repeat", "email", "username"}
    assert data["email"] == "new@example.test"
    assert (
        data["username"]
        == "grimoire-" + hashlib.sha256(b"new@example.test").hexdigest()
    )
    assert context["user_type"] == "external" and context["user_path"] == "grimoire"
    assert "groups" not in context
    assert context["redirect"] == "https://friends.jomcgi.dev/grimoire/join/accept"


@pytest.mark.parametrize(
    "changes",
    [
        {"single_use": False},
        {"flow": "other"},
        {"expires": None},
        {"expires": datetime(2000, 1, 1, tzinfo=timezone.utc)},
        {"username": "chosen"},
        {"email": "bad"},
        {"password": "short"},
    ],
)
def test_policy_rejects_invalid_context(run_policy, changes):
    run, _ = run_policy
    assert run(**changes)[0] is False


def test_existing_identity_never_overwritten(run_policy):
    run, existing = run_policy
    existing.value = True
    assert run()[0] is False
