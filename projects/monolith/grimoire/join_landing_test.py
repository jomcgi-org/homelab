"""The anonymous invitation landing: capture, resume, enrollment and acceptance."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from auth.api import AuthentikStandingVerifier, AuthSettings
from auth.verifier import TokenResolver
from core.db import get_session
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire import access, join_landing, join_links, register
from grimoire.join_landing import JOIN_COOKIE, JoinLinkError, enrollment_url
from grimoire.models import AppUser, CampaignJoinLink, CampaignMember

ISSUER = "https://auth.example.test/application/o/grimoire/"
ORIGIN = "https://friends.example.test"
JOIN = "/grimoire/join"
ACCEPT = "/grimoire/join/accept"
STATE = "/grimoire/join/accept/state"
OTHER_ID = "33333333-3333-4333-8333-333333333333"
FLOW = "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/"
INDEX = (
    '<!doctype html><html><head><script type="module" crossorigin '
    'src="/grimoire/assets/index-abc123.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)
METADATA_KEYS = {
    "id",
    "campaign_id",
    "campaign_name",
    "invitee_email",
    "expires_at",
    "status",
    "can_enroll",
}


@pytest.fixture(name="table")
def table_fixture(tmp_path, monkeypatch):
    """A registered owner and player, one campaign, and a signed-cookie client."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'join-landing.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {table: table.schema for table in SQLModel.metadata.tables.values()}
    for table in schemas:
        table.schema = None
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "grimoire", "alg": "RS256"})

    async def fetch(_url):
        return {"keys": [jwk]}

    verifier = AuthentikStandingVerifier(
        AuthSettings(
            authentik_jwks_url=f"{ISSUER}jwks/",
            authentik_issuer=ISSUER,
            authentik_audience="grimoire-friends",
            jwks_cache_ttl_s=300,
        ),
        fetch=fetch,
    )
    monkeypatch.setattr(access, "grimoire_verifier", lambda: verifier)
    monkeypatch.setenv("GRIMOIRE_BROWSER_ORIGIN", ORIGIN)
    monkeypatch.setenv("GRIMOIRE_AUTH_ISSUER", ISSUER)
    monkeypatch.delenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", raising=False)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text(INDEX)
    monkeypatch.setenv("GRIMOIRE_WEB_DIR", str(tmp_path / "web"))

    def identity(user, **overrides):
        claims = {
            "iss": ISSUER,
            "sub": user,
            "aud": "grimoire-friends",
            "exp": int(time.time()) + 300,
            "email": f"{user}@example.test",
            "name": user,
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "grimoire"})

    def session():
        with Session(engine) as value:
            yield value

    try:
        SQLModel.metadata.create_all(engine)
        app = FastAPI()
        register(app)
        app.dependency_overrides[get_session] = session
        app.state.auth_resolver = TokenResolver([])
        with TestClient(app, base_url=ORIGIN) as client:
            for user in ("owner", "player", "other"):
                registered = client.get(
                    "/api/grimoire/lobby", headers={"x-grimoire-token": identity(user)}
                )
                assert registered.status_code == 200, registered.text
            campaign = client.post(
                "/api/grimoire/campaigns",
                headers={"x-grimoire-token": identity("owner")},
                json={"name": "A table"},
            )
            assert campaign.status_code == 200, campaign.text
            client.cookies.clear()
            yield SimpleNamespace(
                client=client,
                engine=engine,
                identity=identity,
                campaign_id=campaign.json()["id"],
            )
    finally:
        for table, schema in schemas.items():
            table.schema = schema
        engine.dispose()


def issue(table, email="player@example.test", *, enrollment=False):
    """Issue a link as the owner. Enrollment links skip the operator check."""
    with Session(table.engine) as session:
        owner = session.exec(
            select(AppUser).where(AppUser.email == "owner@example.test")
        ).one()
        link = join_links.issue_link(
            session,
            table.campaign_id,
            owner,
            email,
            allow_enrollment=enrollment,
            can_administer_accounts=enrollment,
        )
        return SimpleNamespace(id=link.id, token=link.token)


def revoke(table, link):
    with Session(table.engine) as session:
        owner = session.exec(
            select(AppUser).where(AppUser.email == "owner@example.test")
        ).one()
        join_links.revoke_link(session, table.campaign_id, link.id, owner)


def members(table):
    with Session(table.engine) as session:
        return len(session.exec(select(CampaignMember)).all())


def send(
    table,
    method,
    path,
    body=None,
    *,
    resume=None,
    user=None,
    origin=ORIGIN,
    form=False,
    headers=(),
):
    """One request with exactly the cookies named, as a browser tab holds them."""
    table.client.cookies.clear()
    cookies = []
    if resume is not None:
        cookies.append(f"{JOIN_COOKIE}={resume}")
    if user is not None:
        cookies.append(f"{access.BROWSER_COOKIE}={table.identity(user)}")
    sent = list(headers)
    if cookies:
        sent.append(("cookie", "; ".join(cookies)))
    if origin is not None:
        sent.append(("origin", origin))
    options = {}
    if body is not None:
        options = {"data": body} if form else {"json": body}
    return table.client.request(
        method, path, headers=sent, follow_redirects=False, **options
    )


def post(table, body, **options):
    return send(table, "POST", JOIN, body, **options)


def set_cookies(response):
    return response.headers.get_list("set-cookie")


def assert_cookie_kept(response):
    assert set_cookies(response) == []


def assert_cookie_cleared(response):
    (cookie,) = set_cookies(response)
    assert cookie.startswith((f'{JOIN_COOKIE}="";', f"{JOIN_COOKIE}=;"))
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"max-age=0", "path=/grimoire/join", "httponly", "secure"} <= attributes
    assert "samesite=lax" in attributes


def assert_cookie_saved(response, token):
    (cookie,) = set_cookies(response)
    value, *attributes = [part.strip() for part in cookie.split(";")]
    assert value == f"{JOIN_COOKIE}={token}"
    assert {part.lower() for part in attributes} == {
        "httponly",
        "secure",
        "samesite=lax",
        "path=/grimoire/join",
        "max-age=3600",
    }


def assert_private(response):
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["cloudflare-cdn-cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


@pytest.fixture(name="provider")
def provider_fixture(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", "true")
    state = SimpleNamespace(active=set(), created=[], url=None)

    class MockProvider:
        def exists(self, pk):
            return pk in state.active

        def create(self, **kwargs):
            state.created.append(kwargs)
            pk = str(uuid4())
            state.active.add(pk)
            return pk

        def revoke(self, pk):
            state.active.discard(pk)

        def enrollment_url(self, pk):
            return state.url or f"{FLOW}?itoken={pk}"

    monkeypatch.setattr(join_links, "InvitationProvider", MockProvider)
    return state


# --- the page ---------------------------------------------------------------


def test_landing_is_self_contained_with_a_nonce_only_policy(table):
    response = send(table, "GET", JOIN, origin=None)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert_private(response)
    assert_cookie_kept(response)
    page = response.text
    # No authenticated bundle, external asset or telemetry.
    for forbidden in ('src="', "_app/", "assets/", "otel", "@import", "url(", "http:"):
        assert forbidden not in page
    assert '<meta name="referrer" content="same-origin">' in page
    assert '<meta name="robots" content="noindex,nofollow">' in page
    policy = response.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9+/]{24})'", policy).group(1)
    assert policy == (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "connect-src 'self'; form-action 'self' https://auth.jomcgi.dev; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    assert "unsafe" not in policy
    assert page.count(f'<script nonce="{nonce}">') == 1
    assert page.count(f'<style nonce="{nonce}">') == 1
    assert page.count("<script") == 1 and page.count("<style") == 1
    assert 'fetch("/grimoire/join"' in page
    assert "Opening your invitation…" in page


def test_every_response_carries_a_fresh_nonce(table):
    nonces = {
        re.search(
            r"'nonce-([^']+)'",
            send(table, "GET", JOIN, origin=None).headers["content-security-policy"],
        ).group(1)
        for _ in range(5)
    }
    assert len(nonces) == 5


def test_landing_keeps_the_ids_and_wording_existing_checks_rely_on(table):
    page = send(table, "GET", JOIN, origin=None).text
    for fragment in (
        "<title>Campaign invitation · Grimoire</title>",
        "<h1>Join a campaign</h1>",
        '<p id="status" role="status">',
        '<section id="invitation" aria-label="Invitation details" hidden>',
        '<h2 id="campaign"></h2>',
        '<dd id="recipient"></dd>',
        '<dd id="expires"></dd>',
        '<dd id="link-status"></dd>',
        '<div id="actions" hidden>',
        '<a id="sign-in" class="button" href="/grimoire/join/accept">Sign in to accept</a>',
        '<form id="enrollment" method="POST" action="/grimoire/join" hidden>',
        '<input type="hidden" name="action" value="enroll">',
        '<input id="enrollment-invitation" type="hidden" name="invitation_id">',
        "<button>Create account</button>",
        '<input type="hidden" name="action" value="cancel">',
        "<button>Close invitation</button>",
        "<noscript>",
        '<a href="/grimoire/join">Return to invitation</a>',
    ):
        assert fragment in page, fragment


def test_landing_script_is_the_shipped_file_and_cannot_close_its_element():
    script = join_landing._SCRIPT
    assert script in join_landing.landing("x", enabled=True).body.decode()
    assert "</script" not in script.lower() and "<!--" not in script
    # The token lives only in the fragment and the request body.
    for store in ("localStorage", "sessionStorage", "document.cookie", "innerHTML"):
        assert store not in script
    assert 'window.history.replaceState(null, "", window.location.pathname)' in script
    assert script.index("replaceState") < script.index("fetch(")


def test_messages_are_escaped_into_the_page():
    page = join_landing.landing("<img src=x onerror=1>", enabled=False).body.decode()
    assert "<img" not in page
    assert "&lt;img src=x onerror=1&gt;" in page
    # A failure page never restarts the capture script.
    assert "fetch(" not in page
    assert 'history.replaceState(null,"",location.pathname);' in page


# --- capture and resume ------------------------------------------------------


def test_capture_stores_the_bearer_in_the_cookie_and_returns_only_metadata(table):
    link = issue(table)
    response = post(table, {"action": "capture", "token": link.token})
    assert response.status_code == 200
    assert_private(response)
    body = response.json()
    assert set(body) == METADATA_KEYS
    assert body["id"] == link.id
    assert body["campaign_id"] == table.campaign_id
    assert body["campaign_name"] == "A table"
    assert body["invitee_email"] == "player@example.test"
    assert body["status"] == "pending"
    assert body["can_enroll"] is False
    assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC)
    assert link.token not in response.text
    assert_cookie_saved(response, link.token)
    assert members(table) == 1  # Opening a link grants nothing.


@pytest.mark.parametrize(
    "value",
    ["", "too-short", "../../unsafe", "T" * 129, "T" * 42 + "\n", None, 7, ["T" * 43]],
)
def test_malformed_new_token_clears_stale_resume_state(table, value, monkeypatch):
    monkeypatch.setattr(
        join_links, "inspect_link", lambda *_: pytest.fail("must not inspect")
    )
    stale = issue(table)
    response = post(table, {"action": "capture", "token": value}, resume=stale.token)
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.INCOMPLETE_LINK}
    assert_cookie_cleared(response)


def test_unknown_token_is_refused_with_fixed_text_and_clears_the_cookie(table):
    stale = issue(table)
    unknown = "U" * 43
    response = post(table, {"action": "capture", "token": unknown}, resume=stale.token)
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.INVALID_LINK}
    assert unknown not in response.text and stale.token not in response.text
    assert_cookie_cleared(response)


def test_capture_missing_token_never_falls_back_to_the_cookie(table):
    stale = issue(table)
    response = post(table, {"action": "capture"}, resume=stale.token)
    assert response.status_code == 400
    assert_cookie_cleared(response)


def test_inspect_resumes_from_the_cookie_and_ignores_a_token_in_the_body(table):
    link = issue(table)
    other = issue(table, "other@example.test")
    response = post(
        table, {"action": "inspect", "token": other.token}, resume=link.token
    )
    assert response.status_code == 200
    assert response.json()["id"] == link.id
    assert_cookie_saved(response, link.token)
    assert link.token not in response.text and other.token not in response.text


def test_inspect_without_a_resume_cookie_is_refused(table):
    link = issue(table)
    response = post(table, {"action": "inspect", "token": link.token})
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.INCOMPLETE_LINK}
    assert_cookie_kept(response)


def test_accepted_link_is_shown_but_not_kept(table):
    link = issue(table)
    accepted = send(
        table,
        "POST",
        ACCEPT,
        {"invitation_id": link.id},
        resume=link.token,
        user="player",
    )
    assert accepted.status_code == 200
    for action, options in (
        ("capture", {}),
        ("inspect", {"resume": link.token}),
    ):
        body = {"action": action, "token": link.token}
        response = post(table, body, **options)
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        assert_cookie_cleared(response)


@pytest.mark.parametrize("status", ["accepted", "expired", "revoked"])
def test_metadata_for_a_link_that_is_not_pending_never_sets_the_cookie(
    table, status, monkeypatch
):
    link = issue(table)
    real = join_links.inspect_link
    monkeypatch.setattr(
        join_links,
        "inspect_link",
        lambda session, token: real(session, token).model_copy(
            update={"status": status}
        ),
    )
    response = post(table, {"action": "capture", "token": link.token})
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert_cookie_cleared(response)


def test_revoked_and_expired_links_expose_no_metadata(table):
    revoked = issue(table)
    revoke(table, revoked)
    expired = issue(table, "other@example.test")
    with Session(table.engine) as session:
        row = session.get(CampaignJoinLink, expired.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(row)
        session.commit()
    for link in (revoked, expired):
        response = post(table, {"action": "capture", "token": link.token})
        assert response.status_code == 400
        assert response.json() == {"error": join_landing.INVALID_LINK}
        assert "example.test" not in response.text
        assert_cookie_cleared(response)
        resumed = post(table, {"action": "inspect"}, resume=link.token)
        assert resumed.status_code == 400
        # A failed resume leaves the cookie for the hour it has left.
        assert_cookie_kept(resumed)


def test_metadata_is_an_allowlist_even_if_the_view_grows(table):
    link = issue(table)
    with Session(table.engine) as session:
        view = join_links.inspect_link(session, link.token)
    grown = view.model_copy(
        update={"token": link.token, "provider_invitation_id": "provider-secret"}
    )
    metadata = join_landing.join_metadata(grown)
    assert set(metadata) == METADATA_KEYS
    pending = join_landing.join_metadata(
        view.model_copy(update={"enrollment_cleanup_pending": True})
    )
    assert pending["enrollment_cleanup_pending"] is True


# --- nothing is reflected ----------------------------------------------------


@pytest.mark.parametrize("form", [False, True])
def test_refusal_details_that_might_hold_a_secret_are_never_reflected(
    table, form, monkeypatch
):
    link = issue(table)

    def refuse(*_):
        raise HTTPException(500, detail=link.token)

    monkeypatch.setattr(join_links, "inspect_link", refuse)
    response = post(table, {"action": "capture", "token": link.token}, form=form)
    assert response.status_code == 400
    assert link.token not in response.text
    assert "unavailable right now" in response.text


@pytest.mark.parametrize("action", ["capture", "inspect", "enroll"])
def test_unexpected_errors_are_neither_shown_nor_logged(
    table, action, monkeypatch, caplog
):
    link = issue(table)

    def explode(*_):
        raise RuntimeError(
            f"Cannot fetch https://user:secret@backend.test/{link.token}"
        )

    monkeypatch.setattr(join_links, "inspect_link", explode)
    response = post(
        table,
        {"action": action, "token": link.token, "invitation_id": link.id},
        resume=link.token,
    )
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.JOIN_UNAVAILABLE}
    for secret in ("secret", "backend.test", link.token):
        assert secret not in response.text
        assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_safe_messages_are_fixed_text_only():
    safe = join_landing.safe_join_message
    assert safe(JoinLinkError("Owned by the app.")) == "Owned by the app."
    listed = "Sign in with the account this invitation was created for."
    assert safe(HTTPException(403, listed)) == listed
    for error in (
        HTTPException(403, "sign in to register with Grimoire first"),
        HTTPException(500, {"token": "T" * 43}),
        RuntimeError(listed),
        ValueError("https://user:secret@backend.test"),
    ):
        assert safe(error) == join_landing.JOIN_UNAVAILABLE


# --- origin, size and shape --------------------------------------------------


def test_cross_origin_capture_reads_and_changes_nothing(table, monkeypatch):
    monkeypatch.setattr(
        join_links, "inspect_link", lambda *_: pytest.fail("must not inspect")
    )
    link = issue(table)
    response = post(
        table, {"action": "capture", "token": link.token}, origin="https://evil.test"
    )
    assert response.status_code == 403
    assert response.json() == {"error": join_landing.WRONG_ORIGIN}
    assert_private(response)
    assert_cookie_kept(response)


@pytest.mark.parametrize("action", ["cancel", "enroll"])
@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://evil.test",
        f"{ORIGIN}:8443",
        f"{ORIGIN}/",
        ORIGIN.replace("https", "http"),
        ORIGIN.upper(),
        "",
        None,
    ],
)
def test_native_forms_need_the_exact_origin(table, provider, action, origin):
    link = issue(table, "new@example.test", enrollment=True)
    response = post(
        table,
        {"action": action, "invitation_id": link.id},
        form=True,
        resume=link.token,
        origin=origin,
    )
    assert response.status_code == 403
    assert response.headers["referrer-policy"] == "same-origin"
    assert '<meta name="referrer" content="same-origin">' in response.text
    assert "Open this invitation from the original link." in response.text
    assert "location" not in response.headers
    assert_cookie_kept(response)
    assert provider.created == []


def test_a_repeated_origin_header_fails_closed(table):
    response = post(
        table,
        {"action": "cancel"},
        origin=None,
        headers=[("origin", ORIGIN), ("origin", "https://evil.test")],
    )
    assert response.status_code == 403
    assert_cookie_kept(response)


def test_configured_origin_wins_over_the_host_header(table, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_BROWSER_ORIGIN", "https://real.example.test")
    refused = post(table, {"action": "cancel"}, origin=ORIGIN)
    assert refused.status_code == 403
    allowed = post(table, {"action": "cancel"}, origin="https://real.example.test")
    assert allowed.status_code == 200


def test_without_a_configured_origin_the_request_origin_is_required(table, monkeypatch):
    monkeypatch.delenv("GRIMOIRE_BROWSER_ORIGIN")
    assert post(table, {"action": "cancel"}, origin=ORIGIN).status_code == 200
    for origin in ("https://evil.test", ORIGIN.replace("https", "http"), None):
        assert post(table, {"action": "cancel"}, origin=origin).status_code == 403


@pytest.mark.parametrize("form", [False, True])
def test_oversized_bodies_are_refused_before_parsing(table, form, monkeypatch):
    monkeypatch.setattr(
        join_links, "inspect_link", lambda *_: pytest.fail("must not inspect")
    )
    body = {"action": "capture", "token": "T" * 43, "padding": "x" * 2048}
    response = post(table, body, form=form)
    assert response.status_code == 413
    assert "Invalid invitation request." in response.text
    assert "xxxx" not in response.text
    assert_cookie_kept(response)


def test_a_body_at_the_limit_is_read(table):
    body = json.dumps({"action": "cancel", "padding": ""})
    body = body.replace('""', '"' + "x" * (2048 - len(body)) + '"')
    assert len(body) == 2048
    response = send(
        table,
        "POST",
        JOIN,
        headers=[("content-type", "application/json")],
        body=None,
    )
    assert response.status_code == 400  # An empty body is not JSON.
    table.client.cookies.clear()
    response = table.client.post(
        JOIN,
        content=body,
        headers={"content-type": "application/json", "origin": ORIGIN},
    )
    assert response.status_code == 200


def test_an_undeclared_oversized_stream_is_cut_off(table):
    def chunks():
        for _ in range(5):
            yield b"x" * 1024

    table.client.cookies.clear()
    response = table.client.post(
        JOIN,
        content=chunks(),
        headers={"content-type": "application/json", "origin": ORIGIN},
    )
    assert response.status_code == 413
    assert response.json() == {"error": join_landing.INVALID_REQUEST}


def test_a_lying_content_length_header_is_refused(table):
    for value in ("9999", "not-a-number"):
        response = send(
            table,
            "POST",
            JOIN,
            headers=[("content-type", "application/json"), ("content-length", value)],
        )
        assert response.status_code in (400, 413)
        assert_cookie_kept(response)


@pytest.mark.parametrize(
    "content,status,message",
    [
        (b"{", 400, "Invalid invitation request."),
        (b"\xff\xfe", 400, "Invalid invitation request."),
        (b"null", 400, "Invalid invitation action."),
        (b'["capture"]', 400, "Invalid invitation action."),
        (b'"cancel"', 400, "Invalid invitation action."),
        (b"{}", 400, "Invalid invitation action."),
        (b'{"action": "redeem"}', 400, "Invalid invitation action."),
        (b'{"action": ["cancel"]}', 400, "Invalid invitation action."),
    ],
)
def test_malformed_json_requests(table, content, status, message):
    table.client.cookies.clear()
    response = table.client.post(
        JOIN,
        content=content,
        headers={"content-type": "application/json", "origin": ORIGIN},
    )
    assert response.status_code == status
    assert response.json() == {"error": message}
    assert_private(response)
    assert_cookie_kept(response)


def test_unknown_form_action_answers_with_the_page(table):
    marker = "reflected-marker"
    response = post(table, {"action": marker, "token": marker}, form=True)
    assert response.status_code == 400
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "Invalid invitation action." in response.text
    assert marker not in response.text
    assert "unsafe" not in response.headers["content-security-policy"]


def test_json_is_chosen_by_content_type_alone(table):
    table.client.cookies.clear()
    response = table.client.post(
        JOIN,
        content=b'{"action": "cancel"}',
        headers={"content-type": "text/plain", "origin": ORIGIN},
    )
    # Read as a form, the JSON text names no action.
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")


# --- close ------------------------------------------------------------------


def test_form_close_clears_the_cookie_without_signing_anyone_in(table, monkeypatch):
    monkeypatch.setattr(
        join_links, "inspect_link", lambda *_: pytest.fail("must not inspect")
    )
    link = issue(table)
    response = post(table, {"action": "cancel"}, form=True, resume=link.token)
    assert response.status_code == 200
    assert "Invitation closed" in response.text
    assert '<meta name="referrer" content="same-origin">' in response.text
    assert_private(response)
    assert "location" not in response.headers
    assert_cookie_cleared(response)
    # The link itself is untouched: closing is local to this browser.
    with Session(table.engine) as session:
        assert session.get(CampaignJoinLink, link.id).status == "pending"


def test_json_close_answers_ok_and_clears_the_cookie(table):
    link = issue(table)
    response = post(table, {"action": "cancel"}, resume=link.token)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert_private(response)
    assert_cookie_cleared(response)


# --- enrollment --------------------------------------------------------------


def test_enrollment_redirects_only_to_the_dedicated_flow(table, provider):
    link = issue(table, "new@example.test", enrollment=True)
    captured = post(table, {"action": "capture", "token": link.token})
    assert captured.json()["can_enroll"] is True
    response = post(
        table,
        {"action": "enroll", "invitation_id": link.id},
        form=True,
        resume=link.token,
    )
    assert response.status_code == 303
    (created,) = provider.created
    assert created["email"] == "new@example.test" and created["link_id"] == link.id
    (pk,) = provider.active
    assert response.headers["location"] == f"{FLOW}?itoken={pk}"
    assert_private(response)
    assert response.content == b""
    assert_cookie_kept(response)
    assert members(table) == 1


def test_enrollment_uses_the_cookie_and_ignores_a_posted_token(table, provider):
    link = issue(table, "new@example.test", enrollment=True)
    other = issue(table, "newer@example.test", enrollment=True)
    response = post(
        table,
        {"action": "enroll", "invitation_id": link.id, "token": other.token},
        form=True,
        resume=link.token,
    )
    assert response.status_code == 303
    assert [created["link_id"] for created in provider.created] == [link.id]


def test_an_older_tab_cannot_enroll_with_a_newer_tabs_cookie(table, provider):
    link = issue(table, "new@example.test", enrollment=True)
    for selected in (OTHER_ID, "", "not-an-id", f"{link.id}\n", None):
        body = {"action": "enroll"}
        if selected is not None:
            body["invitation_id"] = selected
        response = post(table, body, form=True, resume=link.token)
        assert response.status_code == 400
        assert "Invitation changed" in response.text
        assert "location" not in response.headers
        assert_cookie_kept(response)
    assert provider.created == []


def test_enrollment_without_a_resume_cookie_is_refused(table, provider):
    link = issue(table, "new@example.test", enrollment=True)
    response = post(table, {"action": "enroll", "invitation_id": link.id}, form=True)
    assert response.status_code == 400
    assert "This invitation link is incomplete." in response.text
    assert provider.created == []


def test_interrupted_enrollment_keeps_resume_state_and_offers_sign_in(table, provider):
    link = issue(table, "new@example.test", enrollment=True)
    # Signup finished before the browser came back to the invitation.
    registered = table.client.get(
        "/api/grimoire/lobby", headers={"x-grimoire-token": table.identity("new")}
    )
    assert registered.status_code == 200
    response = post(
        table,
        {"action": "enroll", "invitation_id": link.id},
        form=True,
        resume=link.token,
    )
    assert response.status_code == 400
    assert "An account may already exist" in response.text
    assert 'href="/grimoire/join/accept">Sign in to continue' in response.text
    assert "Ask the campaign owner for a new link" not in response.text
    assert "location" not in response.headers
    assert_cookie_kept(response)
    assert provider.created == []


def test_existing_account_invitation_cannot_become_a_signup(table, provider):
    link = issue(table)
    response = post(
        table,
        {"action": "enroll", "invitation_id": link.id},
        form=True,
        resume=link.token,
    )
    assert response.status_code == 400
    assert "Account creation is not available for this invitation." in response.text
    assert "Sign in to continue" not in response.text
    assert provider.created == []


def test_provider_failure_during_enrollment_is_generic(table, provider, monkeypatch):
    link = issue(table, "new@example.test", enrollment=True)

    def fail(*_):
        raise HTTPException(503, "https://user:secret@auth.example.test")

    monkeypatch.setattr(join_links, "enroll_link", fail)
    response = post(
        table, {"action": "enroll", "invitation_id": link.id}, resume=link.token
    )
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.JOIN_UNAVAILABLE}


BAD_ENROLLMENT_URLS = [
    "https://evil.test/",
    FLOW,
    f"{FLOW}?itoken=invalid",
    f"{FLOW}?itoken=22222222-2222-4222-8222-222222222222&itoken=33333333-3333-4333-8333-333333333333",
    f"{FLOW}?itoken=22222222-2222-4222-8222-222222222222&next=https://evil.test",
    f"{FLOW}?itoken=22222222-2222-4222-8222-222222222222&redirect=https://evil.test",
    "http://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/",
    "http://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222",
    "https://auth.jomcgi.dev/if/flow/other/",
    "https://auth.jomcgi.dev/if/flow/other/?itoken=22222222-2222-4222-8222-222222222222",
    "https://user@auth.jomcgi.dev/if/flow/grimoire-link-enrollment/",
    "https://user@auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222",
    "https://auth.jomcgi.dev.evil.test/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222",
    "https://auth.jomcgi.dev:8443/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222",
    "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/#secret",
    f"{FLOW}?itoken=22222222-2222-4222-8222-222222222222#secret",
    f"{FLOW}?itoken=22222222-2222-4222-8222-222222222222\n",
    f" {FLOW}?itoken=22222222-2222-4222-8222-222222222222",
    "//auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222",
    "javascript:alert(1)",
    "",
    None,
]


@pytest.mark.parametrize("url", BAD_ENROLLMENT_URLS)
def test_enrollment_url_rejects(url):
    with pytest.raises(JoinLinkError):
        enrollment_url(url)


def test_enrollment_url_accepts_the_flow_with_one_invitation_token():
    url = f"{FLOW}?itoken=22222222-2222-4222-8222-222222222222"
    assert enrollment_url(url) == url


@pytest.mark.parametrize("url", ["https://evil.test/", f"{FLOW}?itoken=x&next=//evil"])
def test_an_unexpected_provider_url_is_never_a_redirect(table, provider, url):
    provider.url = url
    link = issue(table, "new@example.test", enrollment=True)
    response = post(
        table,
        {"action": "enroll", "invitation_id": link.id},
        form=True,
        resume=link.token,
    )
    assert response.status_code == 400
    assert "location" not in response.headers
    assert "Account creation is unavailable right now." in response.text
    assert "evil" not in response.text


# --- signed-in acceptance ----------------------------------------------------


def accept(table, link, *, selected=None, **options):
    options.setdefault("resume", link.token)
    options.setdefault("user", "player")
    return send(
        table, "POST", ACCEPT, {"invitation_id": selected or link.id}, **options
    )


def test_accept_joins_once_then_clears_the_resume_cookie(table):
    link = issue(table)
    response = accept(table, link)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert_private(response)
    assert_cookie_cleared(response)
    assert members(table) == 2
    # The same player retrying a finished join succeeds without a second row.
    again = accept(table, link)
    assert again.status_code == 200 and again.json() == {"ok": True}
    assert members(table) == 2


def test_replay_by_another_account_is_refused(table):
    link = issue(table)
    assert accept(table, link).status_code == 200
    response = accept(table, link, user="other")
    assert response.status_code == 400
    assert response.json() == {
        "error": "Sign in with the account this invitation was created for."
    }
    assert_cookie_kept(response)
    assert members(table) == 2


def test_wrong_account_is_refused_and_the_invitation_stays_recoverable(table):
    link = issue(table)
    response = accept(table, link, user="other")
    assert response.status_code == 400
    assert "account this invitation" in response.json()["error"]
    assert_cookie_kept(response)
    assert members(table) == 1
    assert accept(table, link).status_code == 200


def test_replay_after_removal_cannot_restore_access(table):
    link = issue(table)
    assert accept(table, link).status_code == 200
    with Session(table.engine) as session:
        player = session.exec(
            select(AppUser).where(AppUser.email == "player@example.test")
        ).one()
        member = session.exec(
            select(CampaignMember).where(CampaignMember.app_user_id == player.id)
        ).one()
        session.delete(member)
        session.commit()
    response = accept(table, link)
    assert response.status_code == 400
    assert response.json() == {"error": "This invitation is no longer available."}
    assert members(table) == 1


def test_revoked_invitation_cannot_be_accepted(table):
    link = issue(table)
    revoke(table, link)
    response = accept(table, link)
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.INVALID_LINK}
    assert members(table) == 1


@pytest.mark.parametrize("selected", [OTHER_ID, "not-an-id", "", 7, None, ["x"]])
def test_an_older_tab_cannot_accept_a_newer_invitation(table, selected, monkeypatch):
    monkeypatch.setattr(
        join_links, "redeem_link", lambda *_: pytest.fail("must not redeem")
    )
    link = issue(table)
    response = send(
        table,
        "POST",
        ACCEPT,
        {"invitation_id": selected},
        resume=link.token,
        user="player",
    )
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.INVITATION_CHANGED}
    assert_cookie_kept(response)
    assert members(table) == 1


def test_accept_checks_the_id_against_the_cookie_not_another_link(table):
    link = issue(table)
    other = issue(table, "other@example.test")
    response = accept(table, link, selected=other.id)
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.INVITATION_CHANGED}
    assert members(table) == 1


def test_accept_ignores_a_token_in_the_body(table):
    link = issue(table)
    response = send(
        table,
        "POST",
        ACCEPT,
        {"invitation_id": link.id, "token": link.token},
        user="player",
    )
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.INCOMPLETE_LINK}
    assert members(table) == 1


def test_accept_requires_the_signed_in_identity(table):
    link = issue(table)
    anonymous = accept(table, link, user=None)
    assert anonymous.status_code in (401, 403)
    assert link.token not in anonymous.text
    assert_cookie_kept(anonymous)
    table.client.cookies.clear()
    forged = table.client.post(
        ACCEPT,
        json={"invitation_id": link.id},
        headers={
            "cookie": f"{JOIN_COOKIE}={link.token}; {access.BROWSER_COOKIE}=forged",
            "origin": ORIGIN,
            # A projected email is never authority by itself.
            "x-auth-email": "player@example.test",
        },
    )
    assert forged.status_code in (401, 403)
    assert members(table) == 1


@pytest.mark.parametrize("origin", ["https://evil.test", "null", None])
def test_accept_refuses_a_cross_origin_post(table, origin):
    link = issue(table)
    response = accept(table, link, origin=origin)
    assert response.status_code == 403
    assert members(table) == 1


def test_accept_with_a_header_credential_still_needs_the_origin(table):
    link = issue(table)
    table.client.cookies.clear()
    response = table.client.post(
        ACCEPT,
        json={"invitation_id": link.id},
        headers={
            "cookie": f"{JOIN_COOKIE}={link.token}",
            "x-grimoire-token": table.identity("player"),
        },
    )
    assert response.status_code == 403
    assert response.json() == {"error": join_landing.WRONG_ORIGIN}
    assert members(table) == 1


def test_accept_body_rules(table):
    link = issue(table)
    headers = {
        "cookie": f"{JOIN_COOKIE}={link.token}; "
        f"{access.BROWSER_COOKIE}={table.identity('player')}",
        "origin": ORIGIN,
        "content-type": "application/json",
    }
    for content, status in (
        (b"{", 400),
        (b"", 400),
        (json.dumps({"invitation_id": link.id, "pad": "x" * 2048}).encode(), 413),
    ):
        table.client.cookies.clear()
        response = table.client.post(ACCEPT, content=content, headers=headers)
        assert response.status_code == status
        assert response.json() == {"error": join_landing.INVALID_REQUEST}
        assert_cookie_kept(response)
    assert members(table) == 1


def test_accept_never_reflects_an_unexpected_failure(table, monkeypatch, caplog):
    link = issue(table)

    def explode(*_):
        raise RuntimeError(f"https://user:secret@backend.test/{link.token}")

    monkeypatch.setattr(join_links, "redeem_link", explode)
    response = accept(table, link)
    assert response.status_code == 400
    assert response.json() == {"error": join_landing.JOIN_UNAVAILABLE}
    assert_cookie_kept(response)
    for secret in ("secret", link.token):
        assert secret not in caplog.text

    def refuse(*_):
        raise HTTPException(500, detail=link.token)

    monkeypatch.setattr(join_links, "redeem_link", refuse)
    assert link.token not in accept(table, link).text


def test_new_account_recipient_joins_with_the_invited_email(table, provider):
    link = issue(table, "new@example.test", enrollment=True)
    wrong = accept(table, link, user="other")
    assert wrong.status_code == 400
    assert accept(table, link, user="new").json() == {"ok": True}
    assert members(table) == 2


# --- the signed-in accept screen's state -------------------------------------


def state(table, link=None, user="player"):
    return send(
        table, "GET", STATE, resume=link.token if link else None, user=user, origin=None
    )


def test_state_shows_safe_metadata_and_matches_the_signed_in_recipient(table):
    link = issue(table)
    response = state(table, link)
    assert response.status_code == 200
    assert_private(response)
    assert_cookie_kept(response)
    body = response.json()
    assert set(body) == {"invitation", "email", "matches"}
    assert set(body["invitation"]) == METADATA_KEYS
    assert body["invitation"]["id"] == link.id
    assert body["email"] == "player@example.test"
    assert body["matches"] is True
    assert link.token not in response.text
    assert members(table) == 1  # Looking is not joining.


def test_state_email_mismatch_is_advisory(table):
    link = issue(table)
    body = state(table, link, user="other").json()
    assert body["email"] == "other@example.test"
    assert body["matches"] is False
    assert body["invitation"]["invitee_email"] == "player@example.test"


def test_state_compares_emails_without_case_or_padding(table, monkeypatch):
    link = issue(table)
    real = join_links.inspect_link
    monkeypatch.setattr(
        join_links,
        "inspect_link",
        lambda session, token: real(session, token).model_copy(
            update={"invitee_email": " PLAYER@example.test "}
        ),
    )
    assert state(table, link).json()["matches"] is True


def test_state_errors_are_fixed_text(table, monkeypatch):
    missing = state(table)
    assert missing.status_code == 400
    assert missing.json() == {"error": join_landing.INCOMPLETE_LINK}
    link = issue(table)
    revoke(table, link)
    revoked = state(table, link)
    assert revoked.status_code == 400
    assert revoked.json() == {"error": join_landing.INVALID_LINK}
    assert_cookie_kept(revoked)

    def explode(*_):
        raise TypeError("Cannot fetch https://user:secret@backend.test")

    monkeypatch.setattr(join_links, "inspect_link", explode)
    failed = state(table, link)
    assert failed.status_code == 400
    assert "unavailable" in failed.json()["error"]
    assert "secret" not in failed.text


def test_state_requires_the_signed_in_identity(table):
    link = issue(table)
    response = state(table, link, user=None)
    assert response.status_code in (401, 403)
    assert "player@example.test" not in response.text
    assert link.token not in response.text


def test_state_after_acceptance_supports_the_idempotent_retry(table):
    link = issue(table)
    assert accept(table, link).status_code == 200
    assert state(table, link).json()["invitation"]["status"] == "accepted"


# --- routing -----------------------------------------------------------------


def test_this_router_answers_no_get_for_the_accept_page():
    routes = {
        (route.path, method)
        for route in join_landing.router.routes
        for method in route.methods
    }
    assert routes == {
        (JOIN, "GET"),
        (JOIN, "POST"),
        (ACCEPT, "POST"),
        (STATE, "GET"),
    }


def test_the_accept_page_falls_through_to_the_app_shell(table):
    shell = send(table, "GET", ACCEPT, origin=None)
    assert shell.status_code == 200
    assert shell.text == INDEX
    # The landing and the state endpoint are not shadowed by the shell.
    landing = send(table, "GET", JOIN, origin=None)
    assert "Join a campaign" in landing.text and landing.text != INDEX
    assert send(table, "GET", STATE, origin=None).status_code in (401, 403)
    assert send(table, "GET", "/grimoire/sheets", origin=None).text == INDEX


def test_join_routes_stay_out_of_the_api_schema(table):
    paths = table.client.app.openapi()["paths"]
    assert not any(path.startswith(JOIN) for path in paths)
