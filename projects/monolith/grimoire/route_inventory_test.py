"""Every production-registered campaign route must pass the leak matrix."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from grimoire import join_links, search
from grimoire.models import CampaignJoinLink
from grimoire.testing.leak_harness import ROLES, SHEET_BODY, fake_knn, sqlite_harness

PREFIX = "/api/grimoire/campaigns/{campaign_id}"
CHARACTER = "/characters/{player_character_id}/sheets"
VERSION = CHARACTER + "/{sheet_version_id}"


@dataclass(frozen=True)
class Case:
    params: dict = field(default_factory=dict)
    body: dict | None = None
    query: dict = field(default_factory=dict)
    caller: str = "dm"
    success: int = 200
    state: str | None = None
    denials: dict[str, int] = field(default_factory=dict)
    denied_writers: tuple[str, ...] = ("dm", "player_a", "player_b", "no_character")
    read_only: bool = False


# $row.column values resolve against real persisted fixture rows. Bodies are
# valid requests; no denial may pass through FastAPI's validation error (422).
CASES = {
    ("GET", PREFIX + "/inventory"): Case(),
    ("GET", PREFIX + "/inventory/changes"): Case(),
    ("POST", PREFIX + "/inventory"): Case(body={"owner": "party", "name": "Rope"}),
    ("PATCH", PREFIX + "/inventory/{item_id}"): Case(
        params={"item_id": "$item_a.id"},
        body={"quantity": 2, "reason": "Used one"},
        caller="player_a",
        denied_writers=("player_b", "no_character"),
    ),
    ("POST", PREFIX + "/inventory/{item_id}/move"): Case(
        params={"item_id": "$item_a.id"},
        body={"owner": "party"},
        caller="player_a",
        denied_writers=("player_b", "no_character"),
    ),
    ("DELETE", PREFIX + "/inventory/{item_id}"): Case(
        params={"item_id": "$item_party.id"}, success=204
    ),
    ("POST", PREFIX + "/grants/preview"): Case(
        state="play",
        read_only=True,
        body={
            "grants": [
                {
                    "entity_id": "$private.id",
                    "player_character_id": "$character.id",
                    "grant_scope": "partial",
                    "revealed_details": {"clue": "A preview"},
                }
            ]
        },
    ),
    ("GET", PREFIX): Case(),
    ("PATCH", PREFIX + "/settings"): Case(body={"notes_dm_readable_default": True}),
    ("GET", PREFIX + "/notes"): Case(),
    ("POST", PREFIX + "/notes"): Case(
        body={"kind": "character", "title": "New note"},
        caller="player_a",
        denied_writers=("dm", "no_character"),
    ),
    ("GET", PREFIX + "/notes/{note_id}"): Case(params={"note_id": "$note_party.id"}),
    ("PATCH", PREFIX + "/notes/{note_id}"): Case(
        params={"note_id": "$note_private.id"},
        body={"title": "Edited note"},
        caller="player_a",
    ),
    ("DELETE", PREFIX + "/notes/{note_id}"): Case(
        params={"note_id": "$note_private.id"},
        caller="player_a",
        success=204,
    ),
    ("POST", PREFIX + "/characters"): Case(
        body={"character_name": "New PC", "sheet": {}}
    ),
    ("GET", PREFIX + "/characters"): Case(),
    ("POST", PREFIX + "/characters/self"): Case(
        body={"name": "New PC"},
        caller="no_character",
        denials={"player_a": 409, "player_b": 409, "dm": 403},
    ),
    ("GET", PREFIX + CHARACTER): Case(params={"player_character_id": "$character.id"}),
    ("POST", PREFIX + CHARACTER + "/drafts"): Case(
        params={"player_character_id": "$character.id"},
        body=SHEET_BODY,
        caller="player_b",
        state="closed",
    ),
    ("PATCH", PREFIX + VERSION): Case(
        params={
            "player_character_id": "$character.id",
            "sheet_version_id": "$sheet.id",
        },
        body=SHEET_BODY,
        caller="player_b",
    ),
    ("POST", PREFIX + VERSION + "/submit"): Case(
        params={
            "player_character_id": "$character.id",
            "sheet_version_id": "$sheet.id",
        },
        caller="player_b",
        state="submit",
    ),
    ("POST", PREFIX + VERSION + "/approve"): Case(
        params={
            "player_character_id": "$character.id",
            "sheet_version_id": "$sheet.id",
        },
        body={"comment": "Approved"},
        state="submitted",
    ),
    ("POST", PREFIX + VERSION + "/return"): Case(
        params={
            "player_character_id": "$character.id",
            "sheet_version_id": "$sheet.id",
        },
        body={"comment": "Please revise"},
        state="submitted",
    ),
    ("POST", PREFIX + "/bootstrap-dm"): Case(
        body={"email": "$email.dm"},
        caller="operator",
        state="bootstrap",
    ),
    ("POST", PREFIX + "/members"): Case(
        body={"email": "$email.outsider"}, caller="operator"
    ),
    ("GET", PREFIX + "/members"): Case(),
    ("PUT", PREFIX + "/members/{member_id}/character"): Case(
        params={"member_id": "$member_no_character.id"},
        body={"new": {"name": "New PC"}},
    ),
    ("DELETE", PREFIX + "/members/{member_id}"): Case(
        params={"member_id": "$member.id"}, success=204
    ),
    ("POST", PREFIX + "/grants"): Case(
        body={
            "entity_id": "$private.id",
            "player_character_id": "$character.id",
            "grant_scope": "full",
            "granted_in_session": "$campaign_session.id",
        }
    ),
    ("GET", PREFIX + "/grants"): Case(),
    ("POST", PREFIX + "/grants/bulk"): Case(
        body={
            "grants": [
                {
                    "entity_id": "$private.id",
                    "player_character_id": "$character.id",
                    "grant_scope": "name_only",
                }
            ]
        },
        state="play",
    ),
    ("PATCH", PREFIX + "/grants/{grant_id}"): Case(
        params={"grant_id": "$grant.id"},
        body={"grant_scope": "partial"},
    ),
    ("DELETE", PREFIX + "/grants/{grant_id}"): Case(
        params={"grant_id": "$grant.id"}, success=204
    ),
    ("GET", PREFIX + "/entities"): Case(),
    ("GET", PREFIX + "/entities/{entity_id}"): Case(
        params={"entity_id": "$private.id"}
    ),
    ("GET", PREFIX + "/entities/{entity_id}/relationships"): Case(
        params={"entity_id": "$private.id"}
    ),
    ("GET", PREFIX + "/entities/{entity_id}/mentions"): Case(
        params={"entity_id": "$private.id"}
    ),
    ("GET", PREFIX + "/search"): Case(query={"q": "$private.name", "k": 50}),
    ("GET", PREFIX + "/knowledge/search"): Case(query={"q": "knowledge", "k": 50}),
    ("POST", PREFIX + "/sessions"): Case(),
    ("GET", PREFIX + "/sessions"): Case(),
    ("GET", PREFIX + "/sessions/current"): Case(state="play"),
    ("PATCH", PREFIX + "/sessions/{session_id}"): Case(
        params={"session_id": "$campaign_session.id"},
        body={"status": "paused"},
    ),
    ("POST", PREFIX + "/sessions/{session_id}/events"): Case(
        params={"session_id": "$campaign_session.id"},
        body={"kind": "narration", "audience": "table", "body": {"text": "Welcome"}},
        state="play",
    ),
    ("GET", PREFIX + "/sessions/{session_id}/events"): Case(
        params={"session_id": "$campaign_session.id"},
    ),
    ("GET", PREFIX + "/sessions/{session_id}/journal"): Case(
        params={"session_id": "$campaign_session.id"},
    ),
    ("GET", PREFIX + "/journal"): Case(),
    ("POST", PREFIX + "/sessions/{session_id}/rolls"): Case(
        params={"session_id": "$campaign_session.id"},
        body={"formula": "2d6", "visibility": "table"},
        state="play",
        denied_writers=(),
    ),
    ("GET", PREFIX + "/sessions/{session_id}/initiative"): Case(
        params={"session_id": "$campaign_session.id"},
    ),
    ("PUT", PREFIX + "/sessions/{session_id}/initiative"): Case(
        params={"session_id": "$campaign_session.id"},
        body={
            "entries": [
                {"label": "Goblin", "initiative": 12},
                {"label": "Ogre", "initiative": 6, "hidden": True},
            ],
            "hidden_display": "omit",
        },
        state="play",
    ),
    ("POST", PREFIX + "/sessions/{session_id}/initiative/advance"): Case(
        params={"session_id": "$campaign_session.id"},
        body={"direction": "next"},
        state="play",
    ),
    ("DELETE", PREFIX + "/sessions/{session_id}/initiative"): Case(
        params={"session_id": "$campaign_session.id"},
        state="play",
    ),
    ("POST", PREFIX + "/sessions/{session_id}/events/{event_id}/retract"): Case(
        params={"session_id": "$campaign_session.id", "event_id": "$event_table.id"},
    ),
    ("POST", PREFIX + "/invitations"): Case(body={"email": "$email.outsider"}),
    ("GET", PREFIX + "/invitations"): Case(),
    ("DELETE", PREFIX + "/invitations/{invitation_id}"): Case(
        params={"invitation_id": "$invitation.id"},
        success=204,
    ),
    ("POST", PREFIX + "/join-links"): Case(body={"email": "$email.invitee"}),
    ("GET", PREFIX + "/join-links"): Case(),
    ("DELETE", PREFIX + "/join-links/{link_id}"): Case(
        params={"link_id": "$join_link.id"}, success=204
    ),
}

# These endpoints intentionally sit outside the campaign ACL namespace. The
# token is authority for minimal invitation metadata and enrollment; redeem
# additionally requires the bound verified account. Enumerate them exactly so
# adding another capability endpoint cannot silently escape this inventory.
CAPABILITY_CASES = {
    ("POST", "/api/grimoire/join-links/inspect"): "bearer metadata",
    ("POST", "/api/grimoire/join-links/enroll"): "bearer enrollment",
    ("POST", "/api/grimoire/join-links/redeem"): "bearer plus verified recipient",
}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_INVITATION_LINKS_ENABLED", "true")
    monkeypatch.delenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", raising=False)
    with sqlite_harness(tmp_path / "inventory.db") as h:
        monkeypatch.setattr(search, "knn_embeddings", fake_knn)
        links = []
        for key, campaign, owner, recipient in (
            ("join_link", "campaign", "dm", "outsider"),
            ("other_join_link", "other", "other_campaign", "invitee"),
        ):
            token = secrets.token_urlsafe(32)
            row = CampaignJoinLink(
                id=h.token(f"{key}.id", (owner,), identifier=True),
                campaign_id=h.rows[campaign].id,
                recipient_id=h.rows[f"user_{recipient}"].id,
                invitee_email=h.emails[recipient],
                issued_by_id=h.rows[f"user_{owner}"].id,
                token_digest=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                enrollment_allowed=False,
                enrollment_username=f"grimoire-{key}",
            )
            h.rows[key] = row
            links.append(row)
        h.session.add_all(links)
        h.session.commit()
        yield h


CAMPAIGN_SHAPE = re.compile(r"^/api/grimoire/campaigns/\{[^}/]+\}(/|$)")
CAPABILITY_SHAPE = re.compile(r"^/api/grimoire/join-links(/|$)")


def campaign_routes(app):
    routes = set()
    for context in iter_route_contexts(app.routes):
        if not CAMPAIGN_SHAPE.match(context.path):
            continue
        assert isinstance(context.original_route, APIRoute), (
            "Uncovered non-APIRoute under campaign prefix: "
            f"{type(context.original_route).__name__} {context.path}"
        )
        for method in context.methods:
            routes.add((method, context.path))
    return routes


def assert_inventory(app):
    enumerated = campaign_routes(app)
    assert enumerated == set(CASES), (
        f"Missing CASES: {sorted(enumerated - set(CASES))}; "
        f"stale CASES: {sorted(set(CASES) - enumerated)}"
    )
    assert len(enumerated) == 59  # Inventory and initiative routes extend the exact registry.
    capability_routes = set()
    for context in iter_route_contexts(app.routes):
        if not CAPABILITY_SHAPE.match(context.path):
            continue
        assert isinstance(context.original_route, APIRoute), (
            "Uncovered non-APIRoute under join-link prefix: "
            f"{type(context.original_route).__name__} {context.path}"
        )
        capability_routes.update((method, context.path) for method in context.methods)
    assert capability_routes == set(CAPABILITY_CASES), (
        f"Missing capability CASES: {sorted(capability_routes - set(CAPABILITY_CASES))}; "
        f"stale capability CASES: {sorted(set(CAPABILITY_CASES) - capability_routes)}"
    )


def test_route_inventory(harness):
    assert_inventory(harness.app())


def test_campaign_routes_match_renamed_path_param():
    from fastapi import APIRouter, FastAPI

    probed = APIRouter(prefix="/api/grimoire")

    @probed.get("/campaigns/{cid}/notes")
    def notes(cid: str):
        return {"cid": cid}

    app = FastAPI()
    app.include_router(probed)
    assert ("GET", "/api/grimoire/campaigns/{cid}/notes") in campaign_routes(app)


def resolve(h, value):
    if isinstance(value, list):
        return [resolve(h, item) for item in value]
    if isinstance(value, dict):
        return {key: resolve(h, item) for key, item in value.items()}
    if isinstance(value, str) and value.startswith("$"):
        key, attribute = value[1:].split(".")
        return (
            h.emails[attribute] if key == "email" else getattr(h.rows[key], attribute)
        )
    return value


def call(client, h, method, path, case, viewer, *, params=None, query=None):
    values = {"campaign_id": h.rows["campaign"].id, **resolve(h, case.params)}
    values.update(params or {})
    response = client.request(
        method,
        path.format(**values),
        headers=h.headers(viewer),
        params=resolve(h, case.query if query is None else query),
        json=resolve(h, case.body),
    )
    h.assert_no_leak(response, viewer)
    return response


@pytest.mark.parametrize("method,path", CASES, ids=[f"{m} {p}" for m, p in CASES])
def test_campaign_route_matrix(harness, method, path):
    h = harness
    case = CASES[method, path]
    h.prepare(case.state)
    before = h.snapshot()
    with TestClient(h.app()) as client:
        for viewer in ("outsider", "other_campaign"):
            response = call(client, h, method, path, case, viewer)
            assert response.status_code in (403, 404), response.text
            assert h.snapshot() == before, f"{viewer} mutated {method} {path}"
        if case.params:
            response = call(
                client,
                h,
                method,
                path,
                case,
                "other_campaign",
                params={"campaign_id": h.rows["other"].id},
            )
            assert response.status_code in (403, 404), response.text
            assert h.snapshot() == before, f"cross-campaign mutation: {method} {path}"

        if method != "GET":
            denied_viewers = {*case.denied_writers, *case.denials}
            for viewer in sorted(denied_viewers):
                if viewer == case.caller:
                    continue
                response = call(client, h, method, path, case, viewer)
                expected = (
                    (case.denials[viewer],) if viewer in case.denials else (403, 404)
                )
                assert response.status_code in expected, response.text
                assert h.snapshot() == before, f"{viewer} mutated {method} {path}"

        if method == "GET":
            variants = [(None, None, None)]
            if "player_character_id" in case.params:
                variants = [
                    (
                        {"player_character_id": h.rows[key].id},
                        None,
                        key == "character_a",
                    )
                    for key in ("character_a", "character", "other_character")
                ]
            elif "entity_id" in case.params:
                variants = [
                    ({"entity_id": h.rows[key].id}, None, key in ("a_only", "partial"))
                    for key in (
                        "private",
                        "a_only",
                        "b_only",
                        "partial",
                        "name_only",
                        "foreign",
                    )
                ]
            elif path.endswith(("/entities", "/search")):
                variants += [
                    (None, {"q": h.rows[key].name}, None)
                    for key in (
                        "private",
                        "a_only",
                        "b_only",
                        "partial",
                        "name_only",
                        "foreign",
                    )
                ]
            for viewer in (
                "player_a",
                "player_b",
                "no_character",
                "outsider",
                "other_campaign",
            ):
                for params, query, visible_to_a in variants:
                    response = call(
                        client,
                        h,
                        method,
                        path,
                        case,
                        viewer,
                        params=params,
                        query=query,
                    )
                    if viewer == "player_a" and visible_to_a is not None:
                        expected = 200 if visible_to_a else 404
                        assert response.status_code == expected, response.text
                    elif viewer in ("outsider", "other_campaign"):
                        assert response.status_code == 404, response.text
                    elif path.endswith(
                        ("/members", "/grants", "/invitations", "/join-links")
                    ):
                        assert response.status_code == 403, response.text
                    elif "note_id" in case.params and viewer == "no_character":
                        assert response.status_code == 404, response.text
                    elif params is not None:
                        if viewer == "no_character":
                            assert response.status_code == 404, response.text
                        elif "player_character_id" in params:
                            expected = (
                                200
                                if params["player_character_id"]
                                == h.rows["character"].id
                                else 404
                            )
                            assert response.status_code == expected, response.text
                        else:
                            expected = (
                                200
                                if params["entity_id"] == h.rows["b_only"].id
                                else 404
                            )
                            assert response.status_code == expected, response.text
                    else:
                        assert response.status_code == 200, response.text
                    assert h.snapshot() == before

        # Same request and resource ids: every denial above has a real success.
        response = call(client, h, method, path, case, case.caller)
        assert response.status_code == case.success, response.text
        if method == "GET":
            assert response.json(), f"vacuous positive control: {path}"
        elif case.read_only:
            assert h.snapshot() == before
        else:
            assert h.snapshot() != before, f"success did not mutate {method} {path}"


@pytest.mark.parametrize("shape", ("plain", "lowercase", "nested"))
def test_scanner_rejects_foreign_canary(harness, shape):
    token = harness.rows["private"].name
    request = httpx.Request("GET", "https://test/api/grimoire/campaigns/test/entities")
    response = (
        httpx.Response(200, request=request, json={"deep": [{"secret": token}]})
        if shape == "nested"
        else httpx.Response(
            200, request=request, text=token.lower() if shape == "lowercase" else token
        )
    )
    with pytest.raises(AssertionError, match=token) as failure:
        harness.assert_no_leak(response, "player_a")
    assert str(request.url) in str(failure.value)
    harness.assert_no_leak(response, "dm")


def test_scanner_clean_body_and_audience_matrix(harness):
    request = httpx.Request("GET", "https://test/clean")
    for viewer in (*ROLES, "operator"):
        harness.assert_no_leak(
            httpx.Response(200, request=request, json={"ok": True}), viewer
        )
        for token, (allowed, _) in harness.canaries.items():
            response = httpx.Response(
                403, request=request, json={"nested": [token.lower()]}
            )
            if viewer in allowed:
                harness.assert_no_leak(response, viewer)
            else:
                with pytest.raises(AssertionError, match=token):
                    harness.assert_no_leak(response, viewer)


def test_canaries_are_seeded_and_wire_safe(harness):
    tokens = list(harness.canaries)
    assert len(tokens) == 242  # Inventory items and the initiative order add canaries.
    embeddings = [
        row for key, row in harness.rows.items() if key.startswith("embedding_")
    ]
    assert len(embeddings) == 25
    assert all(row.dim == 1024 and len(row.vector) == 1024 for row in embeddings)
    storage = str(harness.snapshot()).casefold()
    for token in tokens:
        assert re.fullmatch(r"[A-Z0-9-]+", token)
        assert token.casefold() in storage, harness.canaries[token]
        assert all(token not in other for other in tokens if other != token)


def test_inventory_guard_names_new_and_stale_routes(harness, monkeypatch):
    app = harness.app()

    def unreviewed():
        return {}

    app.add_api_route(PREFIX + "/unreviewed", unreviewed, methods=["GET"])
    with pytest.raises(AssertionError, match="Missing CASES.*unreviewed"):
        assert_inventory(app)
    app = harness.app()
    monkeypatch.setitem(CASES, ("GET", PREFIX + "/stale"), Case())
    with pytest.raises(AssertionError, match="stale CASES.*stale"):
        assert_inventory(app)


def test_capability_inventory_guard_names_new_and_stale_routes(harness, monkeypatch):
    app = harness.app()

    def unreviewed():
        return {}

    app.add_api_route(
        "/api/grimoire/join-links/unreviewed", unreviewed, methods=["POST"]
    )
    with pytest.raises(AssertionError, match="Missing capability CASES.*unreviewed"):
        assert_inventory(app)
    app = harness.app()
    monkeypatch.setitem(
        CAPABILITY_CASES, ("POST", "/api/grimoire/join-links/stale"), "unreviewed"
    )
    with pytest.raises(AssertionError, match="stale capability CASES.*stale"):
        assert_inventory(app)


@pytest.mark.parametrize("method,path", CAPABILITY_CASES)
def test_capability_routes_reject_unknown_token_without_disclosing_campaigns(
    harness, method, path
):
    before = harness.snapshot()
    with TestClient(harness.app()) as client:
        for viewer in ROLES:
            response = client.request(
                method,
                path,
                headers=harness.headers(viewer),
                json={"token": secrets.token_urlsafe(32)},
            )
            harness.assert_no_leak(response, viewer)
            assert response.status_code in (403, 404), response.text
            assert harness.snapshot() == before


def test_capability_routes_have_explicit_positive_controls(harness, monkeypatch):
    h = harness
    campaign_id = h.rows["campaign"].id
    with TestClient(h.app()) as client:
        issued = client.post(
            f"/api/grimoire/campaigns/{campaign_id}/join-links",
            headers=h.headers("dm"),
            json={"email": h.emails["invitee"]},
        )
        assert issued.status_code == 200, issued.text
        token = issued.json()["token"]
        before = h.snapshot()
        metadata = client.post(
            "/api/grimoire/join-links/inspect", json={"token": token}
        )
        assert metadata.status_code == 200, metadata.text
        # A valid bearer deliberately grants only this minimal projection,
        # even before login. It never grants entities, roster, or other links.
        assert set(metadata.json()) == {
            "id",
            "campaign_id",
            "campaign_name",
            "invitee_email",
            "expires_at",
            "status",
            "enrollment_cleanup_pending",
            "can_enroll",
        }
        assert metadata.json()["campaign_id"] == campaign_id
        assert metadata.json()["invitee_email"] == h.emails["invitee"]
        h.assert_no_leak(metadata, "dm")
        assert token not in metadata.text
        assert h.snapshot() == before
        joined = client.post(
            "/api/grimoire/join-links/redeem",
            headers=h.headers("invitee"),
            json={"token": token},
        )
        assert joined.status_code == 200, joined.text
        assert joined.json() == {"campaign_id": campaign_id, "status": "accepted"}
        assert h.snapshot() != before

        # The provider boundary is mocked; this test performs no enrollment
        # outside the local fixture, and exercises a genuinely allowed POST.
        provider_id = str(uuid4())

        class LocalProvider:
            def create(self, **kwargs):
                assert kwargs["email"] == "new-recipient@example.test"
                return provider_id

            def enrollment_url(self, invitation_id):
                assert invitation_id == provider_id
                return "https://auth.example/test-enrollment"

        monkeypatch.setattr(join_links, "InvitationProvider", LocalProvider)
        monkeypatch.setenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", "true")
        row = h.rows["join_link"]
        enrollment_token = secrets.token_urlsafe(32)
        row.token_digest = hashlib.sha256(enrollment_token.encode()).hexdigest()
        row.invitee_email = "new-recipient@example.test"
        row.recipient_id = None
        row.enrollment_allowed = True
        h.session.commit()
        before = h.snapshot()
        enrollment = client.post(
            "/api/grimoire/join-links/enroll", json={"token": enrollment_token}
        )
        assert enrollment.status_code == 200, enrollment.text
        assert enrollment.json() == {
            "enrollment_url": "https://auth.example/test-enrollment"
        }
        assert h.snapshot() != before
        assert row.enrollment_id == provider_id
        assert row.status == "pending"


@pytest.mark.parametrize(
    "method,path",
    [*CAPABILITY_CASES, *(key for key in CASES if "/join-links" in key[1])],
)
def test_every_join_link_route_fails_closed_when_disabled(
    harness, monkeypatch, method, path
):
    monkeypatch.delenv("GRIMOIRE_INVITATION_LINKS_ENABLED")
    before = harness.snapshot()
    with TestClient(harness.app()) as client:
        for viewer in ROLES:
            response = client.request(
                method,
                path.format(
                    campaign_id=harness.rows["campaign"].id,
                    link_id=harness.rows["join_link"].id,
                ),
                headers=harness.headers(viewer),
                json=(
                    {"email": harness.emails["invitee"]}
                    if path == PREFIX + "/join-links"
                    else {"token": secrets.token_urlsafe(32)}
                ),
            )
            assert response.status_code == 503, response.text
            harness.assert_no_leak(response, viewer)
            assert harness.snapshot() == before
