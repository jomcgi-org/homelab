"""Exercise the native policy source and its activation boundary without writes."""

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

CHART = Path(__file__).resolve().parent


def entries():
    return yaml.load(
        (CHART / "optional/platform-enrollment.yaml").read_text(),
        Loader=yaml.BaseLoader,
    )["entries"]


def expression(name):
    return next(
        entry["attrs"]["expression"]
        for entry in entries()
        if entry.get("identifiers", {}).get("name") == name
    )


def evaluate(name, request):
    source = "def policy(request):\n" + textwrap.indent(expression(name), "    ")
    context = {"ak_message": lambda _: None}
    exec(compile(source, "platform-policy", "exec"), context)  # noqa: S102 - Trusted checked-in policy source.
    return context["policy"](request)


def render(enabled, *, management=None):
    environment = yaml.safe_load((CHART / "values.yaml").read_text())["authentik"][
        "global"
    ]["env"]
    for variable in environment:
        if variable["name"] == "PLATFORM_AUTH_API_URL":
            variable["value"] = "http://monolith.monolith:8000/api/auth/platform"
        elif variable["name"] == "PLATFORM_AUTH_ENROLLMENT_ISSUER":
            variable["value"] = "https://auth.jomcgi.dev/application/o/grimoire/"
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "authentik",
            str(CHART),
            "--set",
            f"platformEnrollment.enabled={str(enabled).lower()}",
            "--set",
            f"platformManagement.enabled={str(enabled if management is None else management).lower()}",
            "--set-json",
            "authentik.global.env=" + json.dumps(environment),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return next(
        document["data"]
        for document in yaml.safe_load_all(result.stdout)
        if document
        and document["kind"] == "ConfigMap"
        and document["metadata"]["name"] == "authentik-mcp-blueprints"
    )


@pytest.mark.parametrize(
    "enrollment,management", [(False, False), (False, True), (True, True)]
)
def test_keyof_references_follow_their_entry_creation(enrollment, management):
    class KeyReference(str):
        pass

    class Loader(yaml.BaseLoader):
        pass

    Loader.add_constructor(
        "!KeyOf", lambda loader, node: KeyReference(loader.construct_scalar(node))
    )

    def references(value):
        if isinstance(value, KeyReference):
            return {str(value)}
        if isinstance(value, dict):
            return set().union(*(references(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(references(item) for item in value))
        return set()

    for name, content in render(enrollment, management=management).items():
        created = set()
        for entry in yaml.load(content, Loader=Loader)["entries"]:
            assert references(entry) <= created, (
                name,
                entry.get("id"),
                references(entry) - created,
            )
            if entry.get("id"):
                created.add(entry["id"])


def test_opt_in_changes_only_grimoire_profile_and_adds_separate_flow():
    before, after = render(False), render(True)
    assert "platform-enrollment.yaml" not in before
    assert set(after) - set(before) == {"platform-enrollment.yaml"}
    assert all(
        before[key] == after[key]
        for key in before
        if key not in ("grimoire-auth.yaml", "mcp-auth.yaml")
    )
    parsed = yaml.load(after["grimoire-auth.yaml"], Loader=yaml.BaseLoader)
    assert any(entry.get("id") == "platform-profile" for entry in parsed["entries"])
    assert not any(
        any(
            word in entry["model"]
            for word in (
                "rbac",
                "group",
                "invitation",
                "token",
                "oauth2provider",
                "application",
            )
        )
        for entry in entries()
    )
    write = next(
        entry["attrs"]
        for entry in entries()
        if entry["model"].endswith("userwritestage")
    )
    assert (
        write["create_users_as_inactive"] == "true"
        and write["create_users_group"] == "null"
    )
    assert (
        yaml.safe_load((CHART / "values.yaml").read_text())["platformEnrollment"][
            "enabled"
        ]
        is False
    )


@pytest.fixture
def harness(monkeypatch):
    for name in (
        "authentik",
        "authentik.core",
        "authentik.core.models",
        "authentik.core.middleware",
        "authentik.flows",
        "authentik.flows.models",
        "requests",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setenv(
        "PLATFORM_AUTH_API_URL", "https://platform.test/api/auth/platform"
    )
    state = SimpleNamespace(
        user=None,
        flow=True,
        status=200,
        requests=[],
        invitation_id=str(uuid4()),
        accepted_subject=None,
    )
    sys.modules["authentik.core.models"].User = SimpleNamespace(
        objects=SimpleNamespace(
            filter=lambda **_: SimpleNamespace(first=lambda: state.user)
        )
    )
    sys.modules[
        "authentik.core.middleware"
    ].SESSION_KEY_IMPERSONATE_USER = "impersonation"
    sys.modules["authentik.flows.models"].Flow = SimpleNamespace(
        objects=SimpleNamespace(
            filter=lambda **_: SimpleNamespace(exists=lambda: state.flow)
        )
    )

    def post(url, **kwargs):
        state.requests.append((url, kwargs))
        return SimpleNamespace(
            status_code=state.status,
            json=lambda: {
                "invitation_id": state.invitation_id,
                "status": "accepted" if state.accepted_subject else "pending",
                "accepted_subject": state.accepted_subject,
                "activation_pending": True,
            },
        )

    sys.modules["requests"].post = post
    plan = SimpleNamespace(
        flow_pk=str(uuid4()),
        context={"groups": ["family"], "user_type": "service_account"},
    )
    state.token = "a" * 43
    state.plan = plan
    state.request = SimpleNamespace(
        context={
            "flow_plan": plan,
            "prompt_data": {
                "username": "player",
                "platform_invitation_token": state.token,
                "groups": ["operators"],
                "is_superuser": True,
            },
        },
        http_request=SimpleNamespace(
            session={}, user=SimpleNamespace(is_anonymous=True, pk=None)
        ),
    )
    return state


def test_invitation_validation_discards_untrusted_fields_and_capability(harness):
    assert evaluate("platform-validate-invitation", harness.request) is True
    context = harness.plan.context
    assert context["platform_invitation_id"] == harness.invitation_id
    assert (
        context["platform_invitation_digest"]
        == hashlib.sha256(harness.token.encode()).hexdigest()
    )
    assert context["user_type"] == "external" and "groups" not in context
    assert harness.request.context["prompt_data"] == {"username": "player"}
    assert harness.token not in str(context)


@pytest.mark.parametrize(
    "case",
    [
        "other_flow",
        "missing",
        "revoked",
        "active_other",
        "inactive_other",
        "service_account",
        "impersonation",
        "accepted_other",
    ],
)
def test_invitation_policy_denies_substitution_and_bad_state(harness, case):
    if case == "other_flow":
        harness.flow = False
    elif case == "missing":
        harness.request.context["prompt_data"].pop("platform_invitation_token")
    elif case == "revoked":
        harness.status = 410
    elif case in ("active_other", "inactive_other"):
        harness.user = SimpleNamespace(
            is_active=case == "active_other", path="unrelated/path", type="external"
        )
    elif case == "service_account":
        harness.request.http_request.user = SimpleNamespace(
            is_anonymous=False,
            is_active=True,
            type="service_account",
            username="player",
        )
    elif case == "impersonation":
        harness.request.http_request.session["impersonation"] = "other"
    else:
        harness.accepted_subject = "another-identity"
    assert evaluate("platform-validate-invitation", harness.request) is False
    assert "platform_invitation_id" not in harness.plan.context


def test_interrupted_inactive_account_resumes_without_password_overwrite(harness):
    harness.user = SimpleNamespace(
        is_active=False,
        path="platform/invitations/" + harness.invitation_id,
        type="external",
        uid="same-player",
    )
    assert evaluate("platform-validate-invitation", harness.request) is True
    assert harness.plan.context["pending_user"] is harness.user
    assert evaluate("platform-needs-account", harness.request) is False
    harness.request.context["prompt_data"] = {
        "password": "attempted-overwrite",
        "password_repeat": "attempted-overwrite",
    }
    assert evaluate("platform-validate-details", harness.request) is False


def test_existing_account_requires_signed_in_exact_username(harness):
    user = SimpleNamespace(
        is_anonymous=False,
        is_active=True,
        username="player",
        type="internal",
        uid="same-player",
    )
    harness.request.http_request.user = user
    assert evaluate("platform-validate-invitation", harness.request) is True
    assert harness.plan.context["pending_user"] is user
    assert evaluate("platform-needs-account", harness.request) is False


def test_details_cannot_write_groups_roles_or_another_username(harness):
    assert evaluate("platform-validate-invitation", harness.request) is True
    harness.request.context["prompt_data"] = {
        "password": "long-test-password",
        "password_repeat": "long-test-password",
        "username": "administrator",
        "groups": ["operators"],
        "is_superuser": True,
        "email": "owner@example.test",
    }
    assert evaluate("platform-validate-details", harness.request) is True
    assert harness.request.context["prompt_data"] == {
        "username": "player",
        "name": "player",
        "password": "long-test-password",
    }


@pytest.mark.parametrize("ack_status", [200, 503])
def test_completion_signs_distinct_bound_phases_and_rolls_back_failed_ack(
    harness, monkeypatch, ack_status
):
    assert evaluate("platform-validate-invitation", harness.request) is True
    for module in (
        "authentik.providers",
        "authentik.providers.oauth2",
        "authentik.providers.oauth2.models",
        "django",
        "django.db",
    ):
        monkeypatch.setitem(sys.modules, module, ModuleType(module))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = SimpleNamespace(
        sub_mode="hashed_user_id",
        signing_key=SimpleNamespace(private_key=key, kid="fixture-key"),
    )
    sys.modules["authentik.providers.oauth2.models"].OAuth2Provider = SimpleNamespace(
        objects=SimpleNamespace(get=lambda **_: provider)
    )
    sys.modules["authentik.providers.oauth2.models"].SubModes = SimpleNamespace(
        HASHED_USER_ID="hashed_user_id"
    )
    issuer = "https://idp.test/grimoire/"
    monkeypatch.setenv("PLATFORM_AUTH_ENROLLMENT_ISSUER", issuer)
    user = SimpleNamespace(
        pk=1,
        uid="player",
        username="player",
        email="",
        type="external",
        is_active=False,
        stored_active=False,
        path="platform/invitations/" + harness.invitation_id,
    )
    user.refresh_from_db = lambda: setattr(user, "is_active", user.stored_active)
    user.save = lambda **_: None
    harness.plan.context["pending_user"] = user

    @contextmanager
    def atomic():
        try:
            yield
        except Exception:
            user.is_active = user.stored_active
            raise
        else:
            user.stored_active = user.is_active

    sys.modules["django.db"].transaction = SimpleNamespace(atomic=atomic)
    receipts = []

    def post(url, **arguments):
        receipts.append(
            jwt.decode(
                arguments["json"]["receipt"],
                key.public_key(),
                algorithms=["RS256"],
                issuer=issuer,
                audience="platform-registration",
            )
        )
        return SimpleNamespace(
            status_code=ack_status if url.endswith("/acknowledge") else 200,
            json=lambda: {"activation_pending": True},
        )

    sys.modules["requests"].post = post
    assert evaluate("platform-complete-registration", harness.request) is (
        ack_status == 200
    )
    assert user.stored_active is (ack_status == 200)
    assert [receipt["phase"] for receipt in receipts] == ["completed", "activated"]
    assert all(
        receipt["invitation_id"] == harness.invitation_id
        and receipt["sub"] == "player"
        and receipt["exp"] - receipt["iat"] == 60
        for receipt in receipts
    )


def test_acknowledged_invitation_cannot_resume_admin_disabled_identity(harness):
    harness.user = SimpleNamespace(
        is_active=False,
        path="platform/invitations/" + harness.invitation_id,
        type="external",
        uid="same-player",
    )
    harness.accepted_subject = harness.user.uid
    sys.modules["requests"].post = lambda *_, **__: SimpleNamespace(
        status_code=200,
        json=lambda: {
            "invitation_id": harness.invitation_id,
            "status": "accepted",
            "accepted_subject": harness.user.uid,
            "activation_pending": False,
        },
    )
    assert evaluate("platform-validate-invitation", harness.request) is False
