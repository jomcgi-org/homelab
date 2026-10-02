"""Notes API privacy, authorization, chip projection, and write boundaries."""

from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from grimoire.models import Note
from grimoire.testing.leak_harness import ROLES, sqlite_harness


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    with sqlite_harness(tmp_path / "notes.db") as h:
        with TestClient(h.app()) as client:
            yield h, client


def url(h, note_id=None, campaign=None):
    base = f"/api/grimoire/campaigns/{campaign or h.rows['campaign'].id}/notes"
    return base + (f"/{note_id}" if note_id is not None else "")


def request(http, method, viewer="player_a", note_id=None, campaign=None, **kwargs):
    h, client = http
    response = client.request(
        method, url(h, note_id, campaign), headers=h.headers(viewer), **kwargs
    )
    h.assert_no_leak(response, viewer)
    return response


def create(http, viewer="player_a", **changes):
    return request(
        http,
        "POST",
        viewer,
        json={"kind": "character", "title": "Travel notes", **changes},
    )


@pytest.mark.parametrize("viewer", ("dm", "player_b"))
@pytest.mark.parametrize("key", ("note_private", "note_shared"))
def test_hidden_character_note_indistinguishable_through_every_route(http, viewer, key):
    if viewer == "dm" and key == "note_shared":
        return
    h, _ = http
    note = h.rows[key]
    before = h.snapshot()
    listed = request(http, "GET", viewer)
    assert listed.status_code == 200
    assert note.id not in {row["id"] for row in listed.json()}
    matched = request(http, "GET", viewer, params={"q": note.title})
    empty = request(http, "GET", viewer, params={"q": "No such note"})
    assert matched.status_code == empty.status_code == 200
    assert matched.json() == empty.json() == []
    for method, args in (
        ("GET", {}),
        ("PATCH", {"json": {"title": "changed"}}),
        ("DELETE", {}),
    ):
        denied = request(http, method, viewer, note.id, **args)
        random = request(http, method, viewer, str(uuid4()), **args)
        foreign = request(http, method, viewer, h.rows["note_foreign"].id, **args)
        assert denied.status_code == random.status_code == foreign.status_code == 404
        assert (
            denied.json()
            == random.json()
            == foreign.json()
            == {"detail": "note not found"}
        )
        assert h.snapshot() == before


@pytest.mark.parametrize("kind", ("character", "party"))
def test_soft_delete_hides_from_every_route_and_is_not_idempotent(http, kind):
    made = create(http, kind=kind, dm_readable=True)
    assert made.status_code == 200
    note_id = made.json()["id"]
    assert request(http, "DELETE", note_id=note_id).status_code == 204
    h, _ = http
    row = h.session.get(Note, note_id)
    assert isinstance(row.deleted_at, datetime)
    for viewer in ("player_a", "dm", "player_b"):
        listed = request(http, "GET", viewer)
        assert note_id not in {row["id"] for row in listed.json()}
        assert request(http, "GET", viewer, params={"q": "Travel notes"}).json() == []
        for method, args in (
            ("GET", {}),
            ("PATCH", {"json": {"pinned": True}}),
            ("DELETE", {}),
        ):
            assert request(http, method, viewer, note_id, **args).status_code == 404


def settings(http, viewer, value):
    h, client = http
    response = client.patch(
        url(h).removesuffix("/notes") + "/settings",
        headers=h.headers(viewer),
        json={"notes_dm_readable_default": value},
    )
    h.assert_no_leak(response, viewer)
    return response


def test_campaign_default_only_applies_when_omitted_and_is_exposed(http):
    h, client = http
    assert create(http).json()["dm_readable"] is False
    assert settings(http, "dm", True).json()["notes_dm_readable_default"] is True
    assert create(http).json()["dm_readable"] is True
    private = create(http, dm_readable=False).json()
    assert private["dm_readable"] is False
    assert create(http, dm_readable=True).json()["dm_readable"] is True
    assert request(http, "GET", "dm", private["id"]).status_code == 404
    response = client.get(url(h).removesuffix("/notes"), headers=h.headers("player_a"))
    assert response.json()["notes_dm_readable_default"] is True
    before = h.snapshot()
    for viewer in (
        "player_a",
        "player_b",
        "no_character",
        "outsider",
        "other_campaign",
    ):
        assert settings(http, viewer, False).status_code in (403, 404)
        assert h.snapshot() == before


def test_note_creation_roles_and_identity_are_server_owned(http):
    h, _ = http
    before = h.snapshot()
    assert create(http, "dm").status_code == 403
    for kind in ("character", "party"):
        assert create(http, "no_character", kind=kind).status_code == 403
        assert h.snapshot() == before
    assert request(http, "GET", "no_character").json() == []
    assert create(http, "dm", kind="party").status_code == 200
    for viewer in ("player_a", "player_b"):
        made = create(http, viewer, kind="party").json()
        member_key = "member_player_a" if viewer == "player_a" else "member"
        assert made["author_member_id"] == h.rows[member_key].id
        assert made["player_character_id"] == h.rows[member_key].player_character_id
    for field in (
        "author_member_id",
        "player_character_id",
        "viewer",
        "as",
        "from_event_id",
    ):
        before = h.snapshot()
        assert (
            request(
                http,
                "POST",
                json={
                    "kind": "character",
                    "title": "Forged",
                    field: str(uuid4()),
                },
            ).status_code
            == 422
        )
        assert h.snapshot() == before
    forged = request(
        http,
        "GET",
        "player_b",
        h.rows["note_private"].id,
        params={"as": "dm", "viewer": h.rows["character_a"].id},
    )
    assert forged.status_code == 404


def test_characterless_author_retains_own_character_note_only(http):
    h, _ = http
    h.rows["member_player_a"].player_character_id = None
    h.session.commit()
    ids = {row["id"] for row in request(http, "GET").json()}
    assert ids == {h.rows["note_private"].id, h.rows["note_shared"].id}
    assert (
        request(
            http,
            "PATCH",
            note_id=h.rows["note_private"].id,
            json={"markdown": "Still mine"},
        ).status_code
        == 200
    )
    assert request(http, "DELETE", note_id=h.rows["note_private"].id).status_code == 204
    assert create(http).status_code == 403


def test_edit_policy_and_immutable_fields(http):
    h, _ = http
    shared = h.rows["note_shared"]
    party = h.rows["note_party"]
    before = h.snapshot()
    for method, args in (("PATCH", {"json": {"title": "edit"}}), ("DELETE", {})):
        assert request(http, method, "dm", shared.id, **args).status_code == 403
        assert request(http, method, "player_b", party.id, **args).status_code == 403
        assert h.snapshot() == before
    assert (
        request(http, "PATCH", "dm", party.id, json={"title": "DM edit"}).status_code
        == 200
    )
    assert (
        request(
            http, "PATCH", note_id=shared.id, json={"dm_readable": False}
        ).status_code
        == 200
    )
    assert request(http, "GET", "dm", shared.id).status_code == 404
    for viewer in ("dm", "player_a"):
        before = h.snapshot()
        assert (
            request(
                http, "PATCH", viewer, party.id, json={"dm_readable": True}
            ).status_code
            == 403
        )
        assert h.snapshot() == before
    for field, value in (
        ("kind", "party"),
        ("author_member_id", str(uuid4())),
        ("created_in_session", str(uuid4())),
    ):
        before = h.snapshot()
        assert (
            request(http, "PATCH", note_id=shared.id, json={field: value}).status_code
            == 422
        )
        assert h.snapshot() == before
    assert request(http, "DELETE", "dm", party.id).status_code == 204


def test_party_projection_omits_author_ids_and_dm_flag(http):
    h, _ = http
    for viewer in ("player_a", "dm", "player_b"):
        response = request(http, "GET", viewer, h.rows["note_party"].id)
        assert response.status_code == 200
        row = response.json()
        assert row["is_mine"] is (viewer == "player_a")
        assert row["can_edit"] is (viewer in ("player_a", "dm"))
        assert ("dm_readable" in row) is (viewer == "player_a")
        assert ("author_member_id" in row) is (viewer != "player_b")
        assert ("player_character_id" in row) is (viewer != "player_b")


def test_character_note_edit_capability_is_author_only(http):
    h, _ = http
    for viewer in ("player_a", "dm"):
        row = request(http, "GET", viewer, h.rows["note_shared"].id).json()
        assert row["can_edit"] is (viewer == "player_a")
        assert ("dm_readable" in row) is (viewer == "player_a")


def test_entity_chips_resolve_for_each_viewer_and_event_ids_stay_opaque(http):
    h, _ = http
    links = {
        "entity_ids": [h.rows[key].id for key in ("a_only", "partial", "name_only")],
        "event_ids": [str(uuid4())],
    }
    made = create(http, kind="party", links=links)
    assert made.status_code == 200, made.text
    note_id = made.json()["id"]
    for viewer in ("player_a", "dm", "player_b"):
        row = request(http, "GET", viewer, note_id).json()
        chips = row["links"]["entities"]
        assert len(chips) == (0 if viewer == "player_b" else 3)
        assert all(set(chip) == {"id", "name", "type"} for chip in chips)
        assert row["links"]["event_ids"] == links["event_ids"]
    # Revoking a grant drops every trace of that entity from the author's wire.
    h.session.delete(h.rows["grant_a_only"])
    h.session.commit()
    row = request(http, "GET", note_id=note_id).json()
    assert h.rows["a_only"].id not in str(row)
    assert h.rows["a_only"].name not in str(row)


@pytest.mark.parametrize("key", ("private", "b_only", "foreign"))
def test_author_cannot_store_invisible_entity_links(http, key):
    h, _ = http
    before = h.snapshot()
    links = {"entity_ids": [h.rows[key].id]}
    denied = create(http, links=links)
    assert denied.status_code == 404
    random = create(http, links={"entity_ids": [str(uuid4())]})
    assert denied.json() == random.json() == {"detail": "entity not found"}
    assert (
        request(
            http, "PATCH", note_id=h.rows["note_private"].id, json={"links": links}
        ).status_code
        == 404
    )
    assert h.snapshot() == before


def test_created_in_session_is_scoped_to_campaign(http):
    h, _ = http
    before = h.snapshot()
    for session_id in (h.rows["other_session"].id, str(uuid4())):
        denied = create(http, created_in_session=session_id)
        assert denied.status_code == 404
        assert denied.json() == {"detail": "session not found"}
        assert h.snapshot() == before
    made = create(http, created_in_session=h.rows["campaign_session"].id)
    assert made.status_code == 200, made.text
    assert made.json()["created_in_session"] == h.rows["campaign_session"].id


@pytest.mark.parametrize("query", ("%", "_", "\\", "100%_\\", "aBC"))
def test_search_matches_like_wildcards_literally_and_case_insensitively(http, query):
    assert create(http, title="ABC 100%_\\ meters").status_code == 200
    assert create(http, title="Unrelated").status_code == 200
    result = request(http, "GET", params={"q": query})
    assert result.status_code == 200
    assert [row["title"] for row in result.json()] == ["ABC 100%_\\ meters"]


def test_search_filtering_precedes_limit_and_orders_pinned_updated_id(http):
    first = create(http, kind="party", title="Match first", pinned=True).json()
    second = create(http, kind="party", title="Match second").json()
    third = create(http, title="Match hidden", pinned=True).json()
    # A hidden pinned note must not consume the DM's limit.
    result = request(http, "GET", "dm", params={"q": "match", "limit": 1}).json()
    assert [row["id"] for row in result] == [first["id"]]
    result = request(http, "GET", params={"q": "match", "kind": "party"}).json()
    assert [row["id"] for row in result] == [first["id"], second["id"]]
    result = request(http, "GET", params={"q": "match", "kind": "character"}).json()
    assert [row["id"] for row in result] == [third["id"]]
    old = first["updated_at"]
    changed = request(http, "PATCH", note_id=first["id"], json={}).json()
    assert changed["updated_at"] > old
    result = request(http, "GET", params={"q": "match", "limit": 500})
    assert result.status_code == 200
    assert request(http, "GET", params={"limit": 501}).status_code == 422


def test_link_deduplication_and_bounds(http):
    h, _ = http
    entity_id = h.rows["a_only"].id
    event_id = str(uuid4())
    made = create(
        http,
        links={
            "entity_ids": [entity_id, entity_id.lower()],
            "event_ids": [event_id] * 55,
        },
    )
    assert made.status_code == 200, made.text
    assert len(made.json()["links"]["entities"]) == 1
    assert made.json()["links"]["event_ids"] == [event_id]
    before = h.snapshot()
    for changes in (
        {"title": "x" * 201},
        {"markdown": "x" * 20001},
        {"links": {"event_ids": [str(uuid4()) for _ in range(51)]}},
        {"links": {"entity_ids": [str(uuid4()) for _ in range(51)]}},
        {"links": {"event_ids": ["not-a-uuid"]}},
        {"links": {"entity_ids": ["bad"]}},
        {"dm_readable": None},
    ):
        assert create(http, **changes).status_code == 422
        assert h.snapshot() == before
    for field in ("title", "markdown", "pinned", "dm_readable", "links"):
        assert (
            request(
                http, "PATCH", note_id=h.rows["note_private"].id, json={field: None}
            ).status_code
            == 422
        )
        assert h.snapshot() == before


@pytest.mark.parametrize(
    "key", ("note_deleted_character", "note_deleted_party", "note_foreign")
)
@pytest.mark.parametrize("viewer", ROLES[:4])
def test_seeded_deleted_and_foreign_notes_have_no_wire_projection(http, key, viewer):
    h, _ = http
    row = h.rows[key]
    before = h.snapshot()
    assert request(http, "GET", viewer, params={"q": row.title}).json() == []
    for method, args in (
        ("GET", {}),
        ("PATCH", {"json": {"pinned": True}}),
        ("DELETE", {}),
    ):
        assert request(http, method, viewer, row.id, **args).status_code == 404
        assert h.snapshot() == before


@pytest.mark.parametrize(
    "links", ({}, {"entity_ids": []}, {"entity_ids": {}, "event_ids": []}, [])
)
def test_note_model_rejects_malformed_link_shape(http, links):
    h, _ = http
    row = Note(
        campaign_id=h.rows["campaign"].id, kind="party", title="Malformed", links=links
    )
    h.session.add(row)
    with pytest.raises(IntegrityError):
        h.session.flush()
    h.session.rollback()


def test_model_defaults_and_note_identity(http):
    h, _ = http
    row = Note(campaign_id=h.rows["campaign"].id, kind="party", title="Default")
    assert row.id is not None
    h.session.add(row)
    h.session.commit()
    assert row.dm_readable is False
    assert row.pinned is False
    assert row.markdown == ""
    assert row.links == {"entity_ids": [], "event_ids": []}
    assert isinstance(row.created_at, datetime)
    assert isinstance(row.updated_at, datetime)
    assert row.deleted_at is None
