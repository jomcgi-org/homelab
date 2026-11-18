"""Exercise factory social flows in a disposable Authentik database."""

from base64 import b32decode
from pathlib import Path
from time import sleep, time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import SESSION_KEY
from django.test import Client
from django.urls import reverse

from authentik.blueprints.v1.importer import Importer
from authentik.core.models import Application, Group, User
from authentik.policies.denied import AccessDeniedResponse
from authentik.policies.engine import PolicyEngine
from authentik.sources.oauth.clients.oauth2 import OAuth2Client
from authentik.stages.authenticator.oath import totp

for filename in ("factory-auth.yaml", "factory-kubectl.yaml", "factory-social.yaml"):
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
    assert importer.validate()[0], filename
    assert importer.apply(), filename


def callback(browser, identifier):
    profile = {
        "id": identifier,
        "username": "friend",
        "email": "friend@example.org",
        "verified": True,
    }
    with (
        patch.object(
            OAuth2Client, "get_access_token", return_value={"access_token": "fixture"}
        ),
        patch.object(OAuth2Client, "get_profile_info", return_value=profile),
    ):
        return browser.get("/source/oauth/callback/discord/")


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


def exercise():
    browser = Client(HTTP_HOST="localhost")
    assert callback(browser, "42").status_code == 302
    completed = advance(browser, browser.get(executor("enrollment")))
    assert completed["component"] == "xak-flow-redirect"
    assert completed["final_redirect"]
    assert completed["to"] == "/source/oauth/login/discord/"
    assert SESSION_KEY not in browser.session
    user = User.objects.get(username="discord-42")
    assert not user.groups.exists()

    waiting = Client(HTTP_HOST="localhost")
    pending = callback(waiting, "42")
    assert isinstance(pending, AccessDeniedResponse)
    assert b"pending a group grant" in pending.content
    assert SESSION_KEY not in waiting.session
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
        assert SESSION_KEY not in browser.session
    # The setup device lives in the server session; read its secret from the
    # otpauth:// URL the challenge gives the user's authenticator app.
    query = parse_qs(urlparse(challenge["config_url"]).query)
    secret = query["secret"][0]
    device = SimpleNamespace(
        bin_key=b32decode(secret + "=" * (-len(secret) % 8)),
        digits=int(query.get("digits", ["6"])[0]),
        step=int(query.get("period", ["30"])[0]),
    )
    submit_code(browser, url, challenge["component"], device)
    user = User.objects.get(username="discord-42")
    assert str(browser.session[SESSION_KEY]) == str(user.pk)
    user.groups.remove(viewers)
    browser = Client(HTTP_HOST="localhost")
    assert callback(browser, "42").status_code == 302
    url = executor("authentication")
    challenge = advance(browser, browser.get(url))
    assert challenge["component"] == "ak-stage-authenticator-validate"
    assert SESSION_KEY not in browser.session

    # TOTP replay protection requires a fresh counter after enrollment.
    sleep(device.step - time() % device.step + 1)
    submit_code(browser, url, challenge["component"], device)
    assert str(browser.session[SESSION_KEY]) == str(user.pk)

    browser = Client(HTTP_HOST="localhost")
    assert isinstance(callback(browser, "43"), AccessDeniedResponse)
    assert not User.objects.filter(username="discord-43").exists()
    assert SESSION_KEY not in browser.session


exercise()
