"""Handout bodies, uploads, the members-only image route and note pinning."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from grimoire import handouts
from grimoire.models import Book, GameSession, KnowledgeChunk, Note, SessionEvent
from grimoire.testing.leak_harness import PNG_BYTES, seed_handouts, sqlite_harness


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "handouts.db") as h:
        h.prepare("play")
        s3 = seed_handouts(h)
        monkeypatch.setattr("grimoire.ingest.build_s3_client", lambda: s3)
        with TestClient(h.app()) as client:
            yield h, client, s3


def base(h):
    return f"/api/grimoire/campaigns/{h.rows['campaign'].id}"


def post_event(env, body, viewer="dm", **extra):
    h, client, _ = env
    payload = {"kind": "handout", "audience": "table", "body": body, **extra}
    return client.post(
        base(h) + f"/sessions/{h.rows['campaign_session'].id}/events",
        headers=h.headers(viewer),
        json=payload,
    )


def chunk_image(h, key):
    return {"source": "chunk", "chunk_id": h.rows[f"chunk_image_{key}"].id}


def upload_key(h, name=None, campaign="campaign"):
    return f"campaigns/{h.rows[campaign].id}/handouts/{name or uuid4().hex}.png"


def test_open_licensed_chunk_image_is_accepted_and_body_normalised(env):
    h = env[0]
    response = post_event(
        env,
        {"title": "  Map  ", "markdown": "text", "image": chunk_image(h, "open")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["body"] == {
        "title": "Map",
        "markdown": "text",
        "image": chunk_image(h, "open"),
    }
    row = h.session.get(SessionEvent, response.json()["id"])
    assert row.body == response.json()["body"]


@pytest.mark.parametrize("key", ("closed", "unclassified"))
def test_copyrighted_or_unclassified_chunk_images_are_refused(env, key):
    h = env[0]
    before = h.snapshot()
    response = post_event(
        env, {"title": "Map", "markdown": "", "image": chunk_image(h, key)}
    )
    assert response.status_code == 422, response.text
    assert h.snapshot() == before


def test_null_copyright_flag_fails_closed(env):
    h = env[0]
    book = h.session.get(Book, h.rows["book_open"].id)
    book.copyrighted_content = None
    assert (
        handouts.open_licensed_image_chunk(h.session, h.rows["chunk_image_open"].id)
        is None
    )
    h.session.rollback()


def test_text_chunk_and_unknown_chunk_are_refused(env):
    h = env[0]
    text_chunk = KnowledgeChunk(book_id="open-book", chunk_ref="text", content="x")
    h.session.add(text_chunk)
    h.session.commit()
    for chunk_id in (text_chunk.id, str(uuid4())):
        response = post_event(
            env,
            {
                "title": "Map",
                "markdown": "",
                "image": {"source": "chunk", "chunk_id": chunk_id},
            },
        )
        assert response.status_code == 422, response.text


def test_upload_key_must_match_this_campaigns_handout_prefix(env):
    h = env[0]
    good = upload_key(h)
    assert (
        post_event(
            env,
            {
                "title": "Map",
                "markdown": "",
                "image": {"source": "upload", "key": good},
            },
        ).status_code
        == 200
    )
    campaign_id = h.rows["campaign"].id
    bad_keys = [
        upload_key(h, campaign="other"),
        f"campaigns/{campaign_id}/handouts/../{uuid4().hex}.png",
        f"campaigns/{campaign_id}/other/{uuid4().hex}.png",
        f"books/{campaign_id}/handouts/{uuid4().hex}.png",
        f"campaigns/{campaign_id}/handouts/{uuid4().hex}.png/extra",
        f"campaigns/{campaign_id}/handouts/{uuid4().hex}.svg",
        f"campaigns/{campaign_id}/handouts/{uuid4().hex.upper()}.png",
        f"campaigns/{campaign_id}/handouts/{uuid4().hex}.png\n",
        f"campaigns/{campaign_id}/handouts/short.png",
        f"s3://grimoire/{good}",
        "/" + good,
    ]
    before = h.snapshot()
    for key in bad_keys:
        response = post_event(
            env,
            {"title": "Map", "markdown": "", "image": {"source": "upload", "key": key}},
        )
        assert response.status_code == 422, (key, response.text)
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "body",
    (
        {"markdown": "no title"},
        {"title": "   ", "markdown": ""},
        {"title": "x" * 201, "markdown": ""},
        {"title": "t", "markdown": "x" * 20001},
        {"title": "t", "markdown": "", "extra": 1},
        {"title": "t", "markdown": "", "entity_id": "not-a-uuid"},
        {"title": "t", "markdown": "", "image": {"source": "url", "url": "http://x"}},
        {"title": "t", "markdown": "", "image": {"source": "chunk"}},
    ),
)
def test_malformed_handout_bodies_are_refused(env, body):
    h = env[0]
    before = h.snapshot()
    assert post_event(env, body).status_code == 422
    assert h.snapshot() == before


def test_entity_must_belong_to_the_campaign(env):
    h = env[0]
    assert (
        post_event(
            env, {"title": "t", "markdown": "", "entity_id": h.rows["foreign"].id}
        ).status_code
        == 422
    )
    assert (
        post_event(
            env, {"title": "t", "markdown": "", "entity_id": h.rows["a_only"].id}
        ).status_code
        == 200
    )
    assert (
        post_event(
            env, {"title": "t", "markdown": "", "entity_id": str(uuid4())}
        ).status_code
        == 422
    )


def test_identical_retry_is_idempotent_and_a_changed_body_conflicts(env):
    h = env[0]
    token = str(uuid4())
    body = {"title": " Map ", "markdown": "m", "image": chunk_image(h, "open")}
    first = post_event(env, body, request_id=token)
    assert first.status_code == 200, first.text
    before = h.snapshot()
    again = post_event(env, dict(body), request_id=token)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert h.snapshot() == before
    changed = post_event(env, {**body, "markdown": "other"}, request_id=token)
    assert changed.status_code == 409
    assert h.snapshot() == before


def test_players_still_cannot_post_handouts(env):
    h = env[0]
    before = h.snapshot()
    response = post_event(env, {"title": "t", "markdown": ""}, viewer="player_a")
    assert response.status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "data,expected",
    (
        (PNG_BYTES, ("image/png", "png")),
        (b"\xff\xd8\xff\xe0rest", ("image/jpeg", "jpg")),
        (b"GIF87a...", ("image/gif", "gif")),
        (b"GIF89a...", ("image/gif", "gif")),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ("image/webp", "webp")),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", None),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None),
        (b"%PDF-1.7", None),
        (b"", None),
    ),
)
def test_sniff_image_uses_magic_bytes(data, expected):
    assert handouts.sniff_image(data) == expected


# --- upload route -----------------------------------------------------------


def upload(env, data, viewer="dm", declared="image/png", campaign=None):
    h, client, _ = env
    return client.post(
        f"/api/grimoire/campaigns/{campaign or h.rows['campaign'].id}/handouts/uploads",
        headers=h.headers(viewer),
        files={"file": ("any.bin", data, declared)},
    )


def test_upload_stores_sniffed_type_whatever_the_client_declares(env):
    h, _, s3 = env
    response = upload(env, PNG_BYTES, declared="application/octet-stream")
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["content_type"] == "image/png"
    assert result["size"] == len(PNG_BYTES)
    assert handouts.valid_upload_key(h.rows["campaign"].id, result["key"])
    assert s3.objects[handouts.handout_bucket(), result["key"]] == (
        PNG_BYTES,
        "image/png",
    )
    # The returned key is exactly what a handout body may reference.
    assert (
        post_event(
            env,
            {
                "title": "t",
                "markdown": "",
                "image": {"source": "upload", "key": result["key"]},
            },
        ).status_code
        == 200
    )


def test_upload_rejects_non_image_bytes_declared_as_png(env):
    h, _, s3 = env
    stored = dict(s3.objects)
    response = upload(env, b"<svg onload=alert(1)/>", declared="image/png")
    assert response.status_code == 415
    assert s3.objects == stored


def test_upload_size_limit_is_inclusive_of_the_cap(env):
    _, _, s3 = env
    pad = handouts.MAX_UPLOAD_BYTES - len(PNG_BYTES)
    exact = upload(env, PNG_BYTES + b"\0" * pad)
    assert exact.status_code == 201, exact.text
    assert exact.json()["size"] == handouts.MAX_UPLOAD_BYTES
    stored = dict(s3.objects)
    over = upload(env, PNG_BYTES + b"\0" * (pad + 1))
    assert over.status_code == 413
    assert s3.objects == stored


def test_upload_is_dm_only_and_hidden_from_outsiders(env):
    h, _, s3 = env
    stored = dict(s3.objects)
    for viewer in ("player_a", "player_b", "no_character"):
        assert upload(env, PNG_BYTES, viewer=viewer).status_code == 403
    for viewer in ("outsider", "other_campaign"):
        assert upload(env, PNG_BYTES, viewer=viewer).status_code == 404
    assert s3.objects == stored


def test_upload_is_hidden_when_play_is_disabled(env, monkeypatch):
    monkeypatch.delenv("GRIMOIRE_PLAY_ENABLED")
    assert upload(env, PNG_BYTES).status_code == 404


# --- image route ------------------------------------------------------------


def image_url(h, event):
    return (
        base(h) + f"/sessions/{h.rows['campaign_session'].id}/events/{event.id}/image"
    )


def get_image(env, viewer, event, **kwargs):
    h, client, _ = env
    return client.get(image_url(h, event), headers=h.headers(viewer), **kwargs)


def test_handout_image_streams_to_recipient_and_dm_without_caching(env):
    h = env[0]
    for viewer in ("dm", "player_a"):
        response = get_image(env, viewer, h.rows["event_handout"])
        assert response.status_code == 200, (viewer, response.text)
        assert response.content == PNG_BYTES
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "viewer", ("player_b", "no_character", "outsider", "other_campaign")
)
def test_handout_image_is_404_for_non_recipients(env, viewer):
    h = env[0]
    missing = get_image(env, viewer, type("E", (), {"id": str(uuid4())}))
    response = get_image(env, viewer, h.rows["event_handout"])
    assert response.status_code == 404
    assert response.json() == missing.json()


def test_retracted_handout_image_is_404_for_players_but_not_the_dm(env):
    h = env[0]
    event = h.rows["event_handout_retracted"]
    assert get_image(env, "player_a", event).status_code == 404
    assert get_image(env, "dm", event).status_code == 200


def test_table_handout_reaches_every_member_with_an_upload_image(env):
    h, _, _ = env
    key = upload(env, PNG_BYTES).json()["key"]
    posted = post_event(
        env,
        {"title": "t", "markdown": "", "image": {"source": "upload", "key": key}},
    ).json()
    event = type("E", (), {"id": posted["id"]})
    for viewer in ("dm", "player_a", "player_b", "no_character"):
        assert get_image(env, viewer, event).status_code == 200, viewer
    for viewer in ("outsider", "other_campaign"):
        assert get_image(env, viewer, event).status_code == 404, viewer


def test_handout_image_is_404_through_another_session_of_the_campaign(env):
    h, client, _ = env
    other = GameSession(campaign_id=h.rows["campaign"].id, status="active")
    h.session.add(other)
    h.session.commit()
    url = base(h) + f"/sessions/{other.id}/events/{h.rows['event_handout'].id}/image"
    assert client.get(url, headers=h.headers("dm")).status_code == 404
    assert client.get(url, headers=h.headers("player_a")).status_code == 404


def test_non_handout_and_imageless_events_have_no_image(env):
    h = env[0]
    imageless = post_event(env, {"title": "t", "markdown": ""}).json()
    assert (
        get_image(env, "dm", type("E", (), {"id": imageless["id"]})).status_code == 404
    )
    narration = h.session.exec(
        select(SessionEvent).where(SessionEvent.kind == "narration")
    ).first()
    assert get_image(env, "dm", narration).status_code == 404


def test_chunk_image_copyright_is_rechecked_at_serve_time(env):
    h = env[0]
    event = h.rows["event_handout"]
    assert get_image(env, "player_a", event).status_code == 200
    book = h.session.get(Book, h.rows["book_open"].id)
    book.copyrighted_content = True
    h.session.commit()
    assert get_image(env, "player_a", event).status_code == 404
    assert get_image(env, "dm", event).status_code == 404


def test_foreign_upload_key_in_a_stored_body_is_not_served(env):
    h, _, s3 = env
    foreign = upload_key(h, campaign="other")
    s3.objects[handouts.handout_bucket(), foreign] = (PNG_BYTES, "image/png")
    event = h.rows["event_handout"]
    event.body = {**event.body, "image": {"source": "upload", "key": foreign}}
    h.session.commit()
    assert get_image(env, "dm", event).status_code == 404


# --- projection and pinning ---------------------------------------------------


def events_for(env, viewer):
    h, client, _ = env
    response = client.get(
        base(h) + f"/sessions/{h.rows['campaign_session'].id}/events",
        headers=h.headers(viewer),
    )
    assert response.status_code == 200, response.text
    h.assert_no_leak(response, viewer)
    return {row["id"]: row for row in response.json()}


def journal_received(env, viewer):
    h, client, _ = env
    response = client.get(
        base(h) + f"/sessions/{h.rows['campaign_session'].id}/journal",
        headers=h.headers(viewer),
    )
    assert response.status_code == 200, response.text
    h.assert_no_leak(response, viewer)
    return {row["id"]: row for row in response.json()["received"]}


def test_entity_id_is_dropped_unless_the_viewer_can_see_the_entity(env):
    h = env[0]
    seeded = h.rows["event_handout"]
    # a_only is granted to player_a only; private is granted to nobody.
    shared = post_event(
        env, {"title": "t", "markdown": "m", "entity_id": h.rows["a_only"].id}
    ).json()
    for source in (events_for, journal_received):
        dm = source(env, "dm")
        assert dm[seeded.id]["body"]["entity_id"] == seeded.body["entity_id"]
        assert dm[shared["id"]]["body"]["entity_id"] == shared["body"]["entity_id"]
        player_a = source(env, "player_a")
        assert "entity_id" not in player_a[seeded.id]["body"]
        assert (
            player_a[shared["id"]]["body"]["entity_id"] == shared["body"]["entity_id"]
        )
        assert player_a[seeded.id]["body"]["title"] == seeded.body["title"]
        player_b = source(env, "player_b")
        assert seeded.id not in player_b
        assert "entity_id" not in player_b[shared["id"]]["body"]
        assert player_b[shared["id"]]["body"]["title"] == "t"
        assert "entity_id" not in source(env, "no_character")[shared["id"]]["body"]


def campaign_journal_received(env, viewer):
    h, client, _ = env
    response = client.get(base(h) + "/journal", headers=h.headers(viewer))
    assert response.status_code == 200, response.text
    h.assert_no_leak(response, viewer)
    rows = {}
    for entry in response.json()["sessions"]:
        rows.update({row["id"]: row for row in entry["journal"]["received"]})
    return rows


def test_image_reference_reaches_only_the_dm_never_the_key_or_chunk_id(env):
    h = env[0]
    key = upload(env, PNG_BYTES).json()["key"]
    uploaded = post_event(
        env,
        {"title": "u", "markdown": "", "image": {"source": "upload", "key": key}},
    ).json()
    chunked = post_event(
        env,
        {"title": "c", "markdown": "", "image": chunk_image(h, "open")},
    ).json()
    sources = (events_for, journal_received, campaign_journal_received)
    for source in sources:
        dm = source(env, "dm")
        assert dm[uploaded["id"]]["body"]["image"] == {"source": "upload", "key": key}
        assert dm[chunked["id"]]["body"]["image"] == chunk_image(h, "open")
    for viewer in ("player_a", "player_b", "no_character"):
        for source in sources:
            seen = source(env, viewer)
            for event_id, source_name in (
                (uploaded["id"], "upload"),
                (chunked["id"], "chunk"),
            ):
                assert seen[event_id]["body"]["image"] == {"source": source_name}
            text = str(seen)
            assert key not in text
            assert "chunk_id" not in text
            assert h.rows["chunk_image_open"].id not in text


def pin(env, viewer, event_id):
    h, client, _ = env
    return client.post(
        base(h) + "/notes",
        headers=h.headers(viewer),
        json={"kind": "character", "from_event_id": event_id},
    )


def test_pin_to_notes_snapshots_the_handout_and_links_only_visible_entities(env):
    h = env[0]
    seeded = h.rows["event_handout"]
    response = pin(env, "player_a", seeded.id)
    assert response.status_code == 200, response.text
    h.assert_no_leak(response, "player_a")
    note = response.json()
    assert note["title"] == seeded.body["title"]
    assert note["markdown"] == seeded.body["markdown"]
    assert note["pinned"] is True
    assert note["links"]["entities"] == []
    assert note["links"]["event_ids"] == [seeded.id]
    assert h.session.get(Note, note["id"]).created_in_session == seeded.session_id

    shared = post_event(
        env, {"title": "Letter", "markdown": "Dear", "entity_id": h.rows["a_only"].id}
    ).json()
    linked = pin(env, "player_a", shared["id"]).json()
    assert [e["id"] for e in linked["links"]["entities"]] == [h.rows["a_only"].id]
    # A pin never fails because the entity is invisible: it just drops the link.
    unlinked = pin(env, "player_b", shared["id"])
    assert unlinked.status_code == 200, unlinked.text
    h.assert_no_leak(unlinked, "player_b")
    assert unlinked.json()["title"] == "Letter"
    assert unlinked.json()["markdown"] == "Dear"
    assert unlinked.json()["links"]["entities"] == []


def test_pin_is_refused_for_non_recipients_and_retracted_handouts(env):
    h = env[0]
    assert pin(env, "player_b", h.rows["event_handout"].id).status_code == 404
    assert pin(env, "player_a", h.rows["event_handout_retracted"].id).status_code == 404


def test_dm_party_note_from_a_handout_links_the_entity(env):
    h, client, _ = env
    response = client.post(
        base(h) + "/notes",
        headers=h.headers("dm"),
        json={"kind": "party", "from_event_id": h.rows["event_handout"].id},
    )
    assert response.status_code == 200, response.text
    assert [e["id"] for e in response.json()["links"]["entities"]] == [
        h.rows["private"].id
    ]
