"""Real event routes, ACL changes, budgets and mocked private object storage."""

import io
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from grimoire import tts
from grimoire.testing.leak_harness import sqlite_harness
from grimoire.tts_runtime import worker

AUDIO = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 36


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_TTS_ENABLED", "true")
    monkeypatch.delenv("GRIMOIRE_TTS_CACHE_ENABLED", raising=False)
    tts.object_cache.cache_clear()
    monkeypatch.setattr(tts, "service", tts.SpeechService())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=AUDIO))
    real_client = httpx.Client
    inference = Mock(
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    monkeypatch.setattr(tts.httpx, "Client", inference)
    with sqlite_harness(tmp_path / "tts.db") as h:
        h.prepare("play")
        h.rows["event_table"].body = {
            **h.rows["event_table"].body,
            "text": "The captain speaks.",
        }
        h.session.commit()
        with TestClient(h.app()) as client:
            yield client, h, inference
    tts.object_cache.cache_clear()


def path(h):
    return f"/api/grimoire/campaigns/{h.rows['campaign'].id}/tts"


def create(client, h, viewer="dm", event="event_table", **extra):
    return client.post(
        path(h), headers=h.headers(viewer), json={"event_id": h.rows[event].id, **extra}
    )


def retrieve(client, h, clip, viewer="dm", event="event_table"):
    return client.get(f"{path(h)}/{h.rows[event].id}/{clip}", headers=h.headers(viewer))


def test_real_routes_success_and_private_audio(setup):
    client, h, inference = setup
    response = create(client, h)
    assert response.status_code == 200, response.text
    clip = response.json()["clip_id"]
    assert len(clip) == 64 and h.rows["private"].id.lower() not in clip
    audio = retrieve(client, h, clip)
    assert audio.status_code == 200, audio.text
    assert audio.headers["cache-control"] == "private, no-store"
    assert audio.json()["audio_base64"] == tts.base64.b64encode(AUDIO).decode()
    assert create(client, h).json() == response.json()
    assert inference.call_count == 1


@pytest.mark.parametrize(
    "viewer,code",
    [
        ("player_a", 403),
        ("no_character", 403),
        ("outsider", 404),
        ("other_campaign", 404),
    ],
)
def test_denied_before_inference_or_cached_read(setup, monkeypatch, viewer, code):
    client, h, inference = setup
    clip = create(client, h).json()["clip_id"]
    inference.reset_mock()
    cache = Mock(side_effect=AssertionError("unauthorized cache access"))
    monkeypatch.setattr(tts.service, "get", cache)
    assert create(client, h, viewer).status_code == code
    assert retrieve(client, h, clip, viewer).status_code == code
    inference.assert_not_called()
    cache.assert_not_called()


@pytest.mark.parametrize(
    "change", ["retract", "membership", "role", "audience", "campaign", "session"]
)
def test_replay_rechecks_current_access(setup, monkeypatch, change):
    client, h, _ = setup
    clip = create(client, h).json()["clip_id"]
    event = h.rows["event_table"]
    if change == "retract":
        event.retracted_at = datetime.now(timezone.utc)
    if change == "membership":
        h.session.delete(h.rows["member_dm"])
    if change == "role":
        h.rows["member_dm"].role = "player"
    if change == "audience":
        event.audience = "dm"
    if change == "campaign":
        event.campaign_id = h.rows["other"].id
    if change == "session":
        event.session_id = h.rows["other_session"].id
    h.session.commit()
    cache = Mock(side_effect=AssertionError("cache access after revocation"))
    monkeypatch.setattr(tts.service, "get", cache)
    assert retrieve(client, h, clip).status_code in (403, 404)
    assert create(client, h).status_code in (403, 404)
    cache.assert_not_called()


def test_cross_campaign_event_and_guess_rejected(setup):
    client, h, inference = setup
    assert create(client, h, event="event_foreign").status_code == 404
    assert retrieve(client, h, "0" * 64).status_code == 404
    inference.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"text": "arbitrary"},
        {"voice": "../../path"},
        {"backend_url": "http://evil.test"},
    ],
)
def test_reject_client_controlled_synthesis(setup, extra):
    client, h, inference = setup
    assert create(client, h, **extra).status_code == 422
    inference.assert_not_called()


def test_disabled_has_no_runtime_or_storage_io(setup, monkeypatch):
    client, h, inference = setup
    monkeypatch.setenv("GRIMOIRE_TTS_ENABLED", "false")
    monkeypatch.setattr(
        tts, "object_cache", Mock(side_effect=AssertionError("cache initialized"))
    )
    assert create(client, h).status_code == 404
    assert retrieve(client, h, "0" * 64).status_code == 404
    inference.assert_not_called()
    tts.object_cache.assert_not_called()


def test_request_and_stored_text_limits(setup):
    client, h, inference = setup
    assert (
        client.post(path(h), headers=h.headers("dm"), content=b" " * 257).status_code
        == 413
    )
    h.rows["event_table"].body = {"text": "x" * (worker.MAX_TEXT + 1)}
    h.session.commit()
    assert create(client, h).status_code == 422
    inference.assert_not_called()


def test_rate_limit_and_concurrency(setup):
    client, h, inference = setup
    assert tts.service.slot.acquire(blocking=False)
    try:
        assert create(client, h).status_code == 429
        inference.assert_not_called()
    finally:
        tts.service.slot.release()
    for _ in range(5):
        assert create(client, h).status_code == 200
    assert create(client, h).status_code == 429


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("timeout"), httpx.ConnectError("offline")]
)
def test_timeout_falls_back_and_releases_slot(setup, failure):
    client, h, inference = setup
    inference.side_effect = failure
    assert create(client, h).status_code == 503
    assert tts.service.slot.acquire(blocking=False)
    tts.service.slot.release()


@pytest.mark.parametrize("audio", [b"bad", b"RIFF" + b"x" * worker.MAX_AUDIO])
def test_audio_limit_and_format(setup, audio):
    client, h, inference = setup
    # setup saved the real class in the mock closure.
    factory = inference.side_effect
    base = factory(timeout=10, trust_env=False)
    inference.side_effect = lambda **kwargs: base
    base._transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=audio)
    )
    assert create(client, h).status_code == 503


def test_mapping_and_identity_invalidation(setup):
    client, h, _ = setup
    event = h.rows["event_table"]
    campaign = h.rows["campaign"].id
    first = tts.select_voice(h.session, campaign, event)
    assert first == tts.select_voice(h.session, campaign, event)
    assert first.identity(campaign) != first.identity(h.rows["other"].id)
    clip = create(client, h).json()["clip_id"]
    response = client.put(
        f"/api/grimoire/campaigns/{campaign}/voices/{event.body['speaker_key']}",
        headers=h.headers("dm"),
        json={"rate": 1.4},
    )
    assert response.status_code == 200
    assert retrieve(client, h, clip).status_code == 404
    h.session.expire_all()
    assert tts.select_voice(h.session, campaign, event).identity(
        campaign
    ) != first.identity(campaign)
    event.body = {"text": "changed", "speaker_key": h.rows["foreign"].id}
    fallback = tts.select_voice(h.session, campaign, event)
    assert fallback.voice == worker.VOICES[0]
    event.body = {"text": "changed", "speaker_key": "Missing label"}
    assert tts.select_voice(h.session, campaign, event) == fallback


def test_model_and_parameters_are_part_of_cache_identity(monkeypatch):
    selection = tts.Selection("hello", worker.VOICES[0], 1, 1, "narrator")
    key = selection.identity("campaign")
    monkeypatch.setattr(tts, "MODEL", "next-model")
    assert selection.identity("campaign") != key
    assert (
        tts.Selection("hello", worker.VOICES[0], 1.1, 1, "narrator").identity(
            "campaign"
        )
        != key
    )


def test_private_cache_expiry_size_and_retention_are_mocked():
    client = Mock()
    cache = tts.R2Cache(client, "test-bucket")
    now = datetime.now(timezone.utc)
    cache.put("digest", AUDIO, now.timestamp())
    put = client.put_object.call_args.kwargs
    assert "ACL" not in put and "digest" in put["Key"]
    assert float(put["Metadata"]["expires"]) == now.timestamp() + 7 * 86400
    for expires, size, expected in [
        (now.timestamp() + 1, len(AUDIO), AUDIO),
        (now.timestamp(), len(AUDIO), None),
        (now.timestamp() + 1, worker.MAX_AUDIO + 1, None),
    ]:
        stream = io.BytesIO(AUDIO)
        client.get_object.return_value = {
            "Body": stream,
            "ContentLength": size,
            "Metadata": {"expires": str(expires)},
        }
        assert cache.get("digest", now.timestamp()) == expected
        assert stream.closed
    client.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "tts/v1/expired", "LastModified": now - timedelta(days=7)},
            {"Key": "tts/v1/fresh", "LastModified": now},
        ],
        "NextContinuationToken": "next",
    }
    assert cache.retention_page(now=now) == "next"
    client.delete_object.assert_called_once_with(
        Bucket="test-bucket", Key="tts/v1/expired"
    )


def test_memory_cache_expiry_and_bound(monkeypatch):
    service = tts.SpeechService()
    monkeypatch.setattr(tts, "MEMORY_BYTES", len(AUDIO))
    service.put("first", AUDIO)
    service.put("second", AUDIO)
    assert service.get("first") is None
    assert service.get("second") == AUDIO
    monkeypatch.setattr(
        tts.time,
        "time",
        lambda: datetime.now(timezone.utc).timestamp() + tts.RETENTION_SECONDS + 1,
    )
    assert service.get("second") is None


def test_cpu_process_uses_plain_stdin_and_timeout(monkeypatch):
    def run(args, **kwargs):
        assert args[0] == "/usr/bin/espeak-ng"
        assert "--stdin" in args and "-m" not in args
        assert kwargs["input"] == b"<hello>"
        assert kwargs["timeout"] == 8
        with open(args[-1], "wb") as stream:
            stream.write(AUDIO)

    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.synthesize("<hello>", worker.VOICES[0], 1, 1) == AUDIO
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired("espeak", 8)),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        worker.synthesize("hello", worker.VOICES[0], 1, 1)


@pytest.mark.parametrize(
    "change,code",
    [
        (None, 200),
        ("model", 422),
        ("voice", 422),
        ("text", 422),
        ("rate", 422),
        ("busy", 429),
    ],
)
def test_worker_request_boundary_is_bounded_and_allowlisted(monkeypatch, change, code):
    import json
    from email.message import Message

    body = {
        "model": worker.MODEL,
        "voice": worker.VOICES[0],
        "text": "Hello",
        "rate": 1,
        "pitch": 1,
    }
    if change == "model":
        body["model"] = "other"
    if change == "voice":
        body["voice"] = "../../file"
    if change == "text":
        body["text"] = "x" * (worker.MAX_TEXT + 1)
    if change == "rate":
        body["rate"] = 100
    request = json.dumps(body).encode()
    handler = worker.Handler.__new__(worker.Handler)
    handler.path = "/synthesize"
    handler.headers = Message()
    handler.headers["Content-Length"] = str(len(request))
    handler.connection = Mock()
    handler.rfile, handler.wfile = io.BytesIO(request), io.BytesIO()
    handler.send_response = Mock()
    handler.send_error = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    infer = Mock(return_value=AUDIO)
    monkeypatch.setattr(worker, "synthesize", infer)
    if change == "busy":
        worker.SLOT.acquire()
    try:
        handler.do_POST()
    finally:
        if change == "busy":
            worker.SLOT.release()
    if code == 200:
        handler.send_response.assert_called_once_with(200)
        assert handler.wfile.getvalue() == AUDIO
        infer.assert_called_once_with("Hello", worker.VOICES[0], 1, 1)
    else:
        handler.send_error.assert_called_once_with(code)
        infer.assert_not_called()
