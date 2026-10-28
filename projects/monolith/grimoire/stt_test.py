"""Offline audio boundary and Google v2 schema contracts. No live credentials."""

import asyncio
import base64
import io
import json
import tempfile
import wave
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import av
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from grimoire.models import (
    SessionEvent,
    SpeechAttempt,
    TranscriptConsent,
)
from grimoire.stt import FakeTranscriber, ProviderUnavailable, Segment, routes
from grimoire.stt.control import FAILURE_THRESHOLD, RATE_PER_MINUTE
from grimoire.stt.google import GoogleTranscriber, parse_response
from grimoire.stt.media import MAX_AUDIO_BYTES, read_audio, validate_media
from grimoire.testing.leak_harness import sqlite_harness


def wav(seconds=1, *, rate=8000):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x01" * int(seconds * rate))
    return output.getvalue()


def webm(seconds=1):
    output = io.BytesIO()
    with av.open(output, "w", format="webm") as container:
        stream = container.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        for offset in range(0, int(seconds * 48000), 960):
            frame = av.AudioFrame(
                format="s16",
                layout="mono",
                samples=min(960, int(seconds * 48000) - offset),
            )
            frame.sample_rate = 48000
            frame.pts = offset
            frame.planes[0].update(b"\0" * frame.planes[0].buffer_size)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_TRANSCRIPT_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_STT_PROVIDER", "google")
    calls = []

    class Recorder(FakeTranscriber):
        async def transcribe(self, audio, mime, lang_hint, phrase_hints):
            calls.append((mime, lang_hint, phrase_hints))
            return await super().transcribe(audio, mime, lang_hint, phrase_hints)

    monkeypatch.setattr(routes, "transcriber", Recorder)

    # Any accidental framework file parser is a regression, even below its
    # spool threshold. Tests never initialize the real provider or ADC.
    def no_spool(*args, **kwargs):
        raise AssertionError("audio must not enter a temporary file")

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", no_spool)
    with sqlite_harness(tmp_path / "stt.db") as h:
        h.prepare("transcript")
        for consent in h.session.exec(select(TranscriptConsent)).all():
            consent.processor = "Google Speech-to-Text"
        h.session.commit()
        with TestClient(h.app()) as client:
            yield h, client, calls


def upload(h, client, *, viewer="player_a", audio=None, mime="audio/wav", key=None):
    return client.post(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/sessions/{h.rows['campaign_session'].id}/audio-segments",
        headers={**h.headers(viewer), "X-Request-ID": str(key or uuid4())},
        files={"audio": ("segment", wav() if audio is None else audio, mime)},
    )


@pytest.mark.parametrize(
    "audio,mime", [(wav(), "audio/wav"), (webm(), "audio/webm;codecs=opus")]
)
def test_media_and_attribution_and_no_spooling(capture, audio, mime, tmp_path, caplog):
    h, client, calls = capture
    before = set(tmp_path.rglob("*"))
    response = upload(h, client, audio=audio, mime=mime)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == h.rows["campaign"].id
    assert body["session_id"] == h.rows["campaign_session"].id
    assert body["body"]["source"] == "browser"
    assert body["body"]["text"] == "A spoken clue"
    assert len(calls) == 1
    assert set(tmp_path.rglob("*")) == before
    serialized = str(h.snapshot()) + caplog.text
    assert base64.b64encode(audio).decode() not in serialized
    assert repr(audio) not in serialized
    h.assert_no_leak(response, "player_a")


def test_replay_is_first_result_without_second_billed_request(capture):
    h, client, calls = capture
    key = uuid4()
    first = upload(h, client, key=key)
    second = upload(h, client, key=key)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "state",
    ["off", "paused", "ended", "no-consent", "wrong-processor", "disabled", "outsider"],
)
def test_rejections_precede_provider(capture, monkeypatch, state):
    h, client, calls = capture
    game = h.rows["campaign_session"]
    if state in {"off", "paused"}:
        game.transcript_state = state
    elif state == "ended":
        game.status = "ended"
    elif state in {"no-consent", "wrong-processor"}:
        for consent in h.session.exec(select(TranscriptConsent)).all():
            if state == "no-consent":
                h.session.delete(consent)
            else:
                consent.processor = "Another processor"
    elif state == "disabled":
        monkeypatch.setenv("GRIMOIRE_TRANSCRIPT_ENABLED", "false")
    h.session.commit()
    response = upload(
        h, client, viewer="outsider" if state == "outsider" else "player_a"
    )
    assert response.status_code in {403, 404, 409}, response.text
    assert calls == []


def test_consent_revoked_during_recognition_cannot_append(capture, monkeypatch):
    h, client, _ = capture
    engine = h.session.get_bind()

    class Revoke(FakeTranscriber):
        async def transcribe(self, *args):
            def revoke():
                from datetime import datetime

                with Session(engine) as session:
                    for row in session.exec(select(TranscriptConsent)).all():
                        row.revoked_at = datetime.now(UTC)
                    session.commit()

            await asyncio.to_thread(revoke)
            return Segment("Must never commit", 0.8, 1)

    monkeypatch.setattr(routes, "transcriber", Revoke)
    assert upload(h, client).status_code == 403
    with Session(engine) as session:
        assert all(
            row.body.get("text") != "Must never commit"
            for row in session.exec(
                select(SessionEvent).where(SessionEvent.kind == "utterance")
            ).all()
        )


def test_hints_use_canonical_visibility_positive_and_negative_controls(capture):
    h, client, calls = capture
    assert upload(h, client).status_code == 200
    hints = " ".join(calls[0][2])
    # The harness places distinct full, partial, name-only, global, DM-only,
    # other-player and cross-campaign canaries. Check its authoritative ACL.
    for token, (allowed, placement) in h.canaries.items():
        if (
            placement.endswith("name")
            and "entity" in placement
            and "player_a" not in allowed
        ):
            assert token.casefold() not in hints.casefold()
    names = []
    for entity, grant in h.session.exec(
        routes.visible_entities_query(h.rows["campaign"].id, h.rows["character_a"].id)
    ).all():
        names.append(entity.name)
    assert names
    for name in names:
        if len(name) <= 100:
            assert name in calls[0][2]


def test_provider_failure_breaker_and_replay(capture, monkeypatch):
    h, client, _calls = capture
    attempts = []

    class Broken:
        async def transcribe(self, *args):
            attempts.append(1)
            raise ProviderUnavailable()

    monkeypatch.setattr(routes, "transcriber", Broken)
    key = uuid4()
    assert upload(h, client, key=key).status_code == 503
    assert upload(h, client, key=key).status_code == 409
    for _ in range(FAILURE_THRESHOLD - 1):
        assert upload(h, client).status_code == 503
    assert upload(h, client).status_code == 503
    assert len(attempts) == FAILURE_THRESHOLD
    status = client.get(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/sessions/{h.rows['campaign_session'].id}/transcript",
        headers=h.headers("player_a"),
    )
    assert status.json()["provider"] == "degraded"


def test_per_member_budget(capture):
    h, client, _calls = capture
    for _ in range(RATE_PER_MINUTE):
        assert upload(h, client).status_code == 200
    assert upload(h, client).status_code == 429
    assert upload(h, client, viewer="player_b").status_code == 200


@pytest.mark.parametrize("seconds", [0.02, 15])
@pytest.mark.parametrize(
    "factory,mime", [(wav, "audio/wav"), (webm, "audio/webm;codecs=opus")]
)
def test_actual_duration_boundaries(factory, mime, seconds):
    assert 0 < validate_media(factory(seconds), mime) <= 15


@pytest.mark.parametrize(
    "audio,mime",
    [
        (wav(15.01), "audio/wav"),
        (webm(15.02), "audio/webm;codecs=opus"),
        (wav()[:-1], "audio/wav"),
        (b"garbage", "audio/wav"),
        (wav(), "audio/webm;codecs=opus"),
        (webm()[:-100], "audio/webm;codecs=opus"),
    ],
)
def test_malformed_wrong_container_and_overduration(audio, mime):
    with pytest.raises(HTTPException):
        validate_media(audio, mime)


@pytest.mark.asyncio
async def test_bounded_request_stream_ignores_content_length():
    class Stream:
        headers: ClassVar[dict] = {
            "content-type": "multipart/form-data; boundary=x",
            "content-length": "1",
        }

        async def stream(self):
            yield b"x" * (MAX_AUDIO_BYTES + 8193)

    with pytest.raises(HTTPException) as error:
        await read_audio(Stream())
    assert error.value.status_code == 413


@pytest.mark.parametrize(
    "mime", ["audio/mpeg", "application/octet-stream", "audio/webm"]
)
def test_wrong_mime(capture, mime):
    h, client, calls = capture
    assert upload(h, client, mime=mime).status_code == 415
    assert not calls


@pytest.mark.asyncio
async def test_google_official_schema_request_and_response(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_STT_PROJECT", "existing-project")
    fixture = json.loads(
        (Path(__file__).parent / "stt/fixtures/recognize.json").read_text()
    )

    def transport(request):
        assert (
            str(request.url)
            == "https://eu-speech.googleapis.com/v2/projects/existing-project/locations/eu/recognizers/_:recognize"
        )
        body = json.loads(request.content)
        assert body["config"]["model"] == "short"
        assert body["config"]["autoDecodingConfig"] == {}
        assert body["config"]["adaptation"] == {
            "phraseSets": [{"inlinePhraseSet": {"phrases": [{"value": "Known name"}]}}]
        }
        assert base64.b64decode(body["content"]) == b"validated audio"
        return httpx.Response(200, json=fixture)

    provider = GoogleTranscriber(
        token_loader=lambda: "test-only", transport=httpx.MockTransport(transport)
    )
    for mime in ("audio/wav", "audio/webm;codecs=opus"):
        segment = await provider.transcribe(
            b"validated audio", mime, "en-GB", ("Known name",)
        )
        assert segment == Segment("A spoken clue", 0.92, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="PRIVATE_AUDIO_HINT_TOKEN"),
        httpx.Response(200, json={"results": [{}]}),
        httpx.Response(200, text="malformed"),
    ],
)
async def test_google_sanitizes_errors(monkeypatch, response, caplog):
    monkeypatch.setenv("GRIMOIRE_STT_PROJECT", "existing-project")
    provider = GoogleTranscriber(
        token_loader=lambda: "PRIVATE_TOKEN",
        transport=httpx.MockTransport(lambda request: response),
    )
    with pytest.raises(ProviderUnavailable) as error:
        await provider.transcribe(
            b"PRIVATE_AUDIO", "audio/wav", "en-GB", ("PRIVATE_HINT",)
        )
    assert str(error.value) == ""
    assert error.value.__context__ is None
    assert "PRIVATE" not in caplog.text


def test_empty_and_unknown_billing_are_not_invented():
    assert parse_response({}) == Segment("", None, None)
    assert (
        parse_response({"metadata": {"totalBilledDuration": "1.5s"}}).billed_seconds
        == 1.5
    )
    with pytest.raises(ValueError):
        parse_response({"metadata": {"totalBilledDuration": "nan"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", [None, "1", "999999999"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_actual_byte_boundary(content_length, extra):
    import struct

    audio = wav()
    padding = MAX_AUDIO_BYTES + extra - len(audio) - 8
    audio += b"JUNK" + struct.pack("<I", padding) + b"\0" * padding
    audio = audio[:4] + struct.pack("<I", len(audio) - 8) + audio[8:]
    payload = (
        b'--x\r\nContent-Disposition: form-data; name="audio"; filename="segment"\r\nContent-Type: audio/wav\r\n\r\n'
        + audio
        + b"\r\n--x--\r\n"
    )

    class Stream:
        def __init__(self):
            self.headers = {"content-type": "multipart/form-data; boundary=x"}
            if content_length is not None:
                self.headers["content-length"] = content_length

        async def stream(self):
            for offset in range(0, len(payload), 8192):
                yield payload[offset : offset + 8192]

    if extra:
        with pytest.raises(HTTPException) as error:
            await read_audio(Stream())
        assert error.value.status_code == 413
    else:
        actual, mime = await read_audio(Stream())
        assert len(actual) == MAX_AUDIO_BYTES
        assert validate_media(actual, mime) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,preamble,status",
    [(b"", b"", 422), (b"\r\n--x--\r\n", b"p" * 8192 + b"\r\n", 413)],
)
async def test_malformed_or_excessive_multipart_framing(suffix, preamble, status):
    class Stream:
        headers: ClassVar[dict] = {"content-type": "multipart/form-data; boundary=x"}

        async def stream(self):
            yield (
                preamble
                + b'--x\r\nContent-Disposition: form-data; name="audio"\r\nContent-Type: audio/wav\r\n\r\n'
                + wav()
                + suffix
            )

    with pytest.raises(HTTPException) as error:
        await read_audio(Stream())
    assert error.value.status_code == status


@pytest.mark.parametrize("failure", [False, True])
def test_safe_telemetry_and_exception_no_persistence(
    capture, monkeypatch, tmp_path, caplog, failure
):
    h, client, _calls = capture
    attributes = {}

    class Span:
        def set_attribute(self, key, value):
            attributes[key] = value

    class Tracer:
        @contextmanager
        def start_as_current_span(self, *args, **kwargs):
            assert kwargs == {
                "record_exception": False,
                "set_status_on_exception": False,
            }
            yield Span()

    class Provider(FakeTranscriber):
        async def transcribe(self, *args):
            if failure:
                raise ProviderUnavailable()
            return Segment("PRIVATE_TRANSCRIPT", 0.9, 2.5)

    monkeypatch.setattr(routes, "tracer", Tracer())
    monkeypatch.setattr(routes, "transcriber", Provider)
    before = set(tmp_path.rglob("*"))
    audio = wav()
    result = upload(h, client, audio=audio)
    assert result.status_code == (503 if failure else 200)
    assert set(tmp_path.rglob("*")) == before
    assert attributes["grimoire.session_id"] == h.rows["campaign_session"].id
    assert attributes["grimoire.stt.latency_ms"] > 0
    assert attributes["grimoire.stt.billing"] == ("unknown" if failure else "confirmed")
    if failure:
        assert "grimoire.stt.billed_seconds" not in attributes
        assert "PRIVATE_TRANSCRIPT" not in str(h.snapshot())
    else:
        assert attributes["grimoire.stt.billed_seconds"] == 2.5
    telemetry = str(attributes) + caplog.text
    assert "PRIVATE_TRANSCRIPT" not in telemetry
    assert base64.b64encode(audio).decode() not in telemetry + str(h.snapshot())
    for token in h.canaries:
        if token != h.rows["campaign_session"].id:
            assert token not in telemetry


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_timeout_or_cancellation_retains_only_unknown_receipt(
    capture, monkeypatch, tmp_path, caplog, failure
):
    h, _client, _calls = capture
    audio = wav()

    class Stream:
        headers: ClassVar[dict] = {"content-type": "multipart/form-data; boundary=x"}

        async def stream(self):
            yield (
                b'--x\r\nContent-Disposition: form-data; name="audio"\r\nContent-Type: audio/wav\r\n\r\n'
                + audio
                + b"\r\n--x--\r\n"
            )

    class Provider:
        async def transcribe(self, *args):
            raise failure()

    context = routes.AudioContext(
        h.session.get_bind(), h.emails["player_a"], None, h.rows["member_player_a"].id
    )
    monkeypatch.setattr(routes, "transcriber", Provider)
    before = set(tmp_path.rglob("*"))
    expected = (
        asyncio.CancelledError if failure is asyncio.CancelledError else HTTPException
    )
    with pytest.raises(expected) as error:
        await routes.audio_segment(
            h.rows["campaign"].id,
            h.rows["campaign_session"].id,
            Stream(),
            context,
            uuid4(),
        )
    if failure is TimeoutError:
        assert error.value.status_code == 503
    assert set(tmp_path.rglob("*")) == before
    with Session(h.session.get_bind()) as session:
        assert session.exec(select(SpeechAttempt)).one().state == "unknown"
    assert base64.b64encode(audio).decode() not in caplog.text + str(h.snapshot())


def test_failure_after_cascaded_receipt_is_still_sanitized(capture):
    h, _client, _calls = capture
    from grimoire.stt.control import finish

    with Session(h.session.get_bind()) as session:
        # A membership deletion can already have removed the pending receipt.
        finish(session, str(uuid4()), success=False)
        session.commit()
