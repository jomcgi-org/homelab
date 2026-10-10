"""Handout bodies, uploads, the members-only image route and note pinning."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from grimoire import handouts
from grimoire.models import Book, KnowledgeChunk, Note, SessionEvent
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
    assert handouts.open_licensed_image_chunk(h.session, h.rows["chunk_image_open"].id) is None
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
            {"title": "Map", "markdown": "", "image": {"source": "upload", "key": good}},
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
