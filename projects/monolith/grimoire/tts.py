"""Default-off event audio. Callers authorize before invoking any cache/runtime code."""

import base64
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from grimoire.tts_runtime.worker import MAX_AUDIO, MAX_TEXT, MODEL, VOICES
from grimoire.voices import _find_voice, validate_speaker_key

RETENTION_SECONDS = 7 * 86400
MEMORY_BYTES = 32 * 1024 * 1024


class TtsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID


class ClipView(BaseModel):
    clip_id: str
    audio_base64: str


class AudioView(BaseModel):
    audio_base64: str


def require_enabled():
    if os.getenv("GRIMOIRE_TTS_ENABLED") != "true":
        raise HTTPException(404, "Not found")


@dataclass(frozen=True)
class Selection:
    text: str
    voice: str
    rate: float
    pitch: float
    preset: str

    def identity(self, campaign_id):
        data = [
            campaign_id.lower(),
            self.text,
            self.voice,
            self.rate,
            self.pitch,
            self.preset,
            MODEL,
        ]
        return hashlib.sha256(json.dumps(data, ensure_ascii=True).encode()).hexdigest()


def select_voice(session: Session, campaign_id: str, event) -> Selection:
    text = event.body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise HTTPException(422, "narration text exceeds speech budget")
    key = event.body.get("speaker_key", "narrator")
    try:
        key = validate_speaker_key(session, campaign_id, key)
    except HTTPException:
        key = "narrator"  # Deleted/unavailable NPCs never select a stale preset.
    row = _find_voice(session, campaign_id, key)
    if row is None:
        key = "narrator"
        row = _find_voice(session, campaign_id, key)
    # Browser hints are never passed as executable voice names. The preset's
    # canonical identity chooses a fixed English variant, independent of hash seeds.
    variant = (
        0
        if key == "narrator"
        else int(
            hashlib.sha256(f"{campaign_id.lower()}:{key}".encode()).hexdigest()[:8], 16
        )
        % len(VOICES)
    )
    preset = json.dumps(
        [key, row.voice_hint, row.updated_at.isoformat()] if row else [key]
    )
    return Selection(
        text, VOICES[variant], row.rate if row else 1, row.pitch if row else 1, preset
    )


class R2Cache:
    """Private objects only; instantiated lazily after the enabled + access gates."""

    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def get(self, key, now):
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key="tts/v1/" + key)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        stream = response["Body"]
        try:
            if float(response.get("Metadata", {}).get("expires", "0")) <= now:
                return None
            if response["ContentLength"] > MAX_AUDIO:
                return None
            audio = stream.read(MAX_AUDIO + 1)
            return audio if valid_audio(audio) else None
        finally:
            stream.close()

    def put(self, key, audio, now):
        self.client.put_object(
            Bucket=self.bucket,
            Key="tts/v1/" + key,
            Body=audio,
            ContentType="audio/wav",
            Metadata={"expires": str(now + RETENTION_SECONDS)},
        )

    def retention_page(self, *, now: datetime, continuation=None):
        """Staged explicit sweep; no scheduler or delivered workload invokes this."""
        args = {"Bucket": self.bucket, "Prefix": "tts/v1/", "MaxKeys": 100}
        if continuation:
            args["ContinuationToken"] = continuation
        page = self.client.list_objects_v2(**args)
        for item in page.get("Contents", []):
            if item["LastModified"] <= now - timedelta(seconds=RETENTION_SECONDS):
                self.client.delete_object(Bucket=self.bucket, Key=item["Key"])
        return page.get("NextContinuationToken")


def valid_audio(audio):
    return (
        44 <= len(audio) <= MAX_AUDIO
        and audio.startswith(b"RIFF")
        and audio[8:12] == b"WAVE"
    )


@lru_cache(maxsize=1)
def object_cache():
    if os.getenv("GRIMOIRE_TTS_CACHE_ENABLED") != "true":
        return None
    import boto3
    from botocore.config import Config

    endpoint = os.environ["SEAWEEDFS_S3_ENDPOINT"]
    if not endpoint.startswith("https://"):
        raise ValueError("TTS cache requires HTTPS")
    return R2Cache(
        boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=os.getenv("AWS_REGION", "us-east-1"),
            config=Config(
                s3={"addressing_style": "path"},
                connect_timeout=1,
                read_timeout=2,
                retries={"max_attempts": 0},
            ),
        ),
        os.environ["GRIMOIRE_S3_BUCKET"],
    )


class SpeechService:
    def __init__(self):
        self.lock = threading.Lock()
        self.slot = threading.BoundedSemaphore(1)
        self.rates = OrderedDict()
        self.audio = OrderedDict()
        self.bytes = 0

    def admit(self, member_id):
        now = time.monotonic()
        with self.lock:
            # Bounded identity state. A full active table denies new identities.
            for key in list(self.rates):
                if self.rates[key][-1] <= now - 60:
                    del self.rates[key]
            if member_id not in self.rates and len(self.rates) >= 1024:
                raise HTTPException(429, "speech busy")
            recent = self.rates.setdefault(member_id, deque())
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= 6:
                raise HTTPException(429, "speech rate limit")
            recent.append(now)

    def get(self, key):
        now = time.time()
        with self.lock:
            entry = self.audio.get(key)
            if entry and entry[0] > now:
                self.audio.move_to_end(key)
                return entry[1]
            if entry:
                self.bytes -= len(self.audio.pop(key)[1])
        cache = object_cache()
        if cache:
            try:
                return cache.get(key, now)
            except Exception:
                return None  # Cache availability cannot prevent browser fallback.
        return None

    def put(self, key, audio):
        now = time.time()
        with self.lock:
            previous = self.audio.pop(key, None)
            if previous:
                self.bytes -= len(previous[1])
            self.audio[key] = (now + RETENTION_SECONDS, audio)
            self.bytes += len(audio)
            while self.bytes > MEMORY_BYTES:
                self.bytes -= len(self.audio.popitem(last=False)[1][1])
        cache = object_cache()
        if cache:
            try:
                cache.put(key, audio, now)
            except Exception:
                pass

    def create(self, key, selection):
        if not self.slot.acquire(blocking=False):
            raise HTTPException(429, "speech busy")
        try:
            audio = self.get(key)
            if audio is None:
                try:
                    # Fixed loopback endpoint; never supplied by a browser or event.
                    with httpx.Client(
                        timeout=httpx.Timeout(10, connect=1), trust_env=False
                    ) as client:
                        with client.stream(
                            "POST",
                            "http://127.0.0.1:8094/synthesize",
                            json={
                                "text": selection.text,
                                "voice": selection.voice,
                                "rate": selection.rate,
                                "pitch": selection.pitch,
                                "model": MODEL,
                            },
                        ) as response:
                            response.raise_for_status()
                            chunks, size = [], 0
                            started = time.monotonic()
                            for chunk in response.iter_bytes(chunk_size=65536):
                                if time.monotonic() - started > 10:
                                    raise ValueError("speech timed out")
                                size += len(chunk)
                                if size > MAX_AUDIO:
                                    raise ValueError("speech too large")
                                chunks.append(chunk)
                            audio = b"".join(chunks)
                    if not valid_audio(audio):
                        raise ValueError("invalid speech")
                except (httpx.HTTPError, ValueError) as error:
                    raise HTTPException(503, "speech unavailable") from error
                self.put(key, audio)
            return ClipView(
                clip_id=key, audio_base64=base64.b64encode(audio).decode("ascii")
            )
        finally:
            self.slot.release()

    def retrieve(self, key):
        audio = self.get(key)
        if audio is None:
            raise HTTPException(404, "clip expired")
        return AudioView(audio_base64=base64.b64encode(audio).decode("ascii"))


service = SpeechService()
