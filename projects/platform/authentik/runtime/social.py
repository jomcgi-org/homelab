"""Exercise factory social flows in a disposable Authentik database."""

from base64 import b32decode
from pathlib import Path
from time import sleep, time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import Client
from django.urls import reverse

from authentik.blueprints.v1.importer import Importer
from authentik.core.models import Application, AuthenticatedSession, Group, User
from authentik.policies.denied import AccessDeniedResponse
from authentik.policies.engine import PolicyEngine
from authentik.sources.oauth.clients.oauth2 import OAuth2Client
from authentik.stages.authenticator.oath import totp

for filename in (
    "factory-auth.yaml",
    "factory-kubectl.yaml",
    "factory-social.yaml",
    "bosun-apply.yaml",
):
    importer = Importer.from_string(
        (Path("/fixture/factory-blueprints") / filename).read_text()
    )
    if filename == "factory-social.yaml":
        # An unreconciled item must keep the entire social blueprint pending.
        path = Path("/run/factory-social")
        for provider in ("github", "google", "discord"):
            (path / provider).mkdir(exist_ok=True)
            for key in ("client-id", "client-secret"):
                if provider == "discord" and key == "client-secret":
                    # 2026.5.6 can raise while it logs an unresolved entry,
                    # instead of returning False. Either way it is not valid.
                    try:
                        pending_valid = importer.validate()[0]
                    except Exception:
                        pending_valid = False
                    assert not pending_valid
                (path / provider / key).write_text("fixture")
        importer = Importer.from_string(
            (Path("/fixture/factory-blueprints") / filename).read_text()
        )
    # The worker applies Authentik's default blueprints in the background, so
    # an object a factory blueprint looks up can appear a little later.
    for _ in range(30):
        valid, logs = importer.validate()
        if valid:
            break
        sleep(2)
        importer = Importer.from_string(
            (Path("/fixture/factory-blueprints") / filename).read_text()
        )
    assert valid, (filename, [str(log) for log in logs][:5])
    assert importer.apply(), filename


def sessions(username):
    # The authentik_session cookie is not a plain session key, so the test
    # client cannot read the server session; count signed-in sessions.
    return AuthenticatedSession.objects.filter(user__username=username).count()


def callback(browser, identifier, email="friend@example.org", provider="discord"):
    profile = {
        "id": identifier,
        "username": "friend",
        "email": email,
        "verified_email" if provider == "google" else "verified": True,
        "name": "Friend",
    }
    with (
        patch.object(
            OAuth2Client, "get_access_token", return_value={"access_token": "fixture"}
        ),
        patch.object(OAuth2Client, "get_profile_info", return_value=profile),
    ):
        return browser.get(f"/source/oauth/callback/{provider}/")


def executor(flow):
    return reverse(
        "authentik_api:flow-executor", kwargs={"flow_slug": f"factory-social-{flow}"}
    )


def advance(browser, response):
    # Silent stages redirect back to the same executor URL. Each redirect
    # consumes a stage, so Django's generic redirect-cycle check is unsuitable.
    for _ in range(20):
        if response.status_code != 302:
            assert response.status_code == 200
            return response.json()
        response = browser.get(response.url)
    raise AssertionError("Flow did not reach a challenge or complete")


def submit_code(browser, url, component, device):
    code = str(totp(device.bin_key, digits=device.digits)).zfill(device.digits)
    response = browser.post(
        url, {"component": component, "code": code}, content_type="application/json"
    )
    return advance(browser, response)


def finish_setup(browser, url, challenge):
    # The setup device lives in the server session; read its secret from the
    # otpauth:// URL the challenge gives the user's authenticator app.
    query = parse_qs(urlparse(challenge["config_url"]).query)
    secret = query["secret"][0]
    device = SimpleNamespace(
        bin_key=b32decode(secret + "=" * (-len(secret) % 8)),
        digits=int(query.get("digits", ["6"])[0]),
        step=int(query.get("period", ["30"])[0]),
    )
    completed = submit_code(browser, url, challenge["component"], device)
    return device, completed


def exercise():
    browser = Client(HTTP_HOST="localhost")
    assert callback(browser, "42").status_code == 302
    completed = advance(browser, browser.get(executor("enrollment")))
    assert completed["component"] == "xak-flow-redirect"
    assert completed["final_redirect"]
    assert completed["to"] == "/source/oauth/login/discord/"
    assert sessions("discord-42") == 0
    user = User.objects.get(username="discord-42")
    assert not user.groups.exists()

    waiting = Client(HTTP_HOST="localhost")
    pending = callback(waiting, "42")
    assert isinstance(pending, AccessDeniedResponse)
    assert b"pending a group grant" in pending.content
    assert sessions("discord-42") == 0
    for slug in ("factory-edge", "factory-kubectl"):
        engine = PolicyEngine(Application.objects.get(slug=slug), user)
        assert not engine.build().result.passing

    viewers = Group.objects.get(name="factory-kube-viewers")
    user.groups.add(viewers)

    # Close the first MFA prompt. The next callback must find the original
    # connection, even in a new browser with no flow plan or session.
    url = executor("authentication")
    for _ in range(2):
        browser = Client(HTTP_HOST="localhost")
        assert callback(browser, "42").status_code == 302
        challenge = advance(browser, browser.get(url))
        assert challenge["component"] == "ak-stage-authenticator-totp"
        assert sessions("discord-42") == 0
    device, _ = finish_setup(browser, url, challenge)
    user = User.objects.get(username="discord-42")
    assert sessions("discord-42") == 1
    user.groups.remove(viewers)
    browser = Client(HTTP_HOST="localhost")
    assert callback(browser, "42").status_code == 302
    url = executor("authentication")
    challenge = advance(browser, browser.get(url))
    assert challenge["component"] == "ak-stage-authenticator-validate"
    assert sessions("discord-42") == 1

    # TOTP replay protection requires a fresh counter after enrollment.
    sleep(device.step - time() % device.step + 1)
    submit_code(browser, url, challenge["component"], device)
    assert sessions("discord-42") == 2

    browser = Client(HTTP_HOST="localhost")
    assert isinstance(callback(browser, "43"), AccessDeniedResponse)
    assert not User.objects.filter(username="discord-43").exists()
    assert sessions("discord-43") == 0


exercise()


def exercise_competing_enrollments():
    first = Client(HTTP_HOST="localhost")
    second = Client(HTTP_HOST="localhost")
    email = "race@example.org"
    assert callback(first, "51", email).status_code == 302
    assert callback(second, "52", email.upper(), "google").status_code == 302
    accounts = User.objects.filter(email__iexact=email)
    assert not accounts.exists()

    url = executor("enrollment")
    # Each user-write stage redirects to the executor for the post-write check.
    first_written = first.get(url)
    assert first_written.status_code == 302
    winner = accounts.get(username="discord-51")
    second_written = second.get(url)
    assert second_written.status_code == 302
    assert accounts.get(username="google-52").pk > winner.pk
    assert accounts.count() == 2

    denied = advance(second, second_written)
    assert denied["component"] == "ak-stage-access-denied"
    assert denied["error_message"] == (
        "An account already uses this email. Sign in with that account instead."
    )
    assert advance(first, first_written)["to"] == "/source/oauth/login/discord/"
    assert accounts.get().pk == winner.pk


exercise_competing_enrollments()


def exercise_application():
    browser = Client(HTTP_HOST="localhost")
    url = reverse("authentik_api:flow-executor", kwargs={"flow_slug": "bosun-apply"})
    start = advance(browser, browser.get(url))
    assert start["to"] == "/source/oauth/login/discord/"
    for flow in ("enrollment", "authentication"):
        assert callback(browser, "44", "applicant@example.org").status_code == 302
        challenge = advance(browser, browser.get(executor(flow)))
        if flow == "enrollment":
            assert challenge["to"] == start["to"]
    assert challenge["component"] == "ak-stage-authenticator-totp"
    _, completed = finish_setup(browser, executor("authentication"), challenge)
    assert completed["final_redirect"]
    assert completed["to"] == "/bosun/application-complete"
    user = User.objects.get(username="discord-44")
    assert sessions(user.username) == 1
    assert list(user.all_groups().values_list("name", flat=True)) == [
        "bosun-applicants"
    ]
    edge = Application.objects.get(slug="factory-edge")
    assert not PolicyEngine(edge, user).build().result.passing

    user.groups.set([Group.objects.get(name="discord-operators")])
    assert not User.objects.get(pk=user.pk).is_superuser
    user.groups.set([Group.objects.get(name="authentik Admins")])
    assert {"discord-operators", "bosun-member"} <= set(
        user.all_groups().values_list("name", flat=True)
    )


exercise_application()
