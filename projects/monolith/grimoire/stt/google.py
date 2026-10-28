"""Google v2 synchronous Recognize over REST, with no recognition retries.

Contract sources and offline fixture provenance are recorded in architecture.md.
Auto instrumentation is suppressed for authentication and recognition: HTTP
errors must never capture request bodies, phrase sets, credentials or text.
"""

import asyncio
import base64
import math
import os
import re

import httpx
from opentelemetry.instrumentation.utils import suppress_instrumentation

from grimoire.stt import ProviderUnavailable, Segment

TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 64 * 1024


def request_body(audio, mime, lang_hint, phrase_hints):
    if mime not in {"audio/wav", "audio/webm;codecs=opus"} or lang_hint != "en-GB":
        raise ProviderUnavailable() from None
    config = {
        "autoDecodingConfig": {},
        "model": "short",
        "languageCodes": [lang_hint],
    }
    if phrase_hints:
        config["adaptation"] = {
            "phraseSets": [
                {
                    "inlinePhraseSet": {
                        "phrases": [{"value": name} for name in phrase_hints]
                    }
                }
            ]
        }
    return {"config": config, "content": base64.b64encode(audio).decode("ascii")}


def parse_response(data):
    results = data.get("results", [])
    if not isinstance(results, list):
        raise TypeError()
    text = []
    confidences = []
    for result in results:
        alternative = result["alternatives"][0]
        value = alternative["transcript"]
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError()
        text.append(value.strip())
        confidence = alternative.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
                raise ValueError()
            confidences.append(float(confidence))
    billed = data.get("metadata", {}).get("totalBilledDuration")
    if billed is not None:
        if not isinstance(billed, str) or not re.fullmatch(
            r"\d+(?:\.\d{1,9})?s", billed
        ):
            raise ValueError()
        billed = float(billed[:-1])
        if not math.isfinite(billed) or billed > 60:
            raise ValueError()
    joined = " ".join(text)
    if len(joined) > 4000:
        raise ValueError()
    return Segment(joined, min(confidences) if confidences else None, billed)


class GoogleTranscriber:
    """Created only after request authorization, admission and media validation.

    ADC consumes GOOGLE_APPLICATION_CREDENTIALS when the Operator-backed file
    is mounted, or an already configured workload identity. No identity is
    provisioned here. Authentication work never receives audio.
    """

    def __init__(self, *, token_loader=None, transport=None):
        project = os.getenv("GRIMOIRE_STT_PROJECT", "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
            raise ProviderUnavailable() from None
        self.url = (
            "https://eu-speech.googleapis.com/v2/projects/"
            f"{project}/locations/eu/recognizers/_:recognize"
        )
        self.token_loader = token_loader or self._token
        self.transport = transport

    @staticmethod
    def _token():
        import google.auth
        from google.auth.transport.requests import Request

        # This function runs in its own worker, with a bounded token request.
        # Suppression is also applied here because ContextVars cross to_thread.
        with suppress_instrumentation():
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            request = Request()

            def bounded_request(*args, **kwargs):
                kwargs["timeout"] = 3
                return request(*args, **kwargs)

            credentials.refresh(bounded_request)
            return credentials.token

    async def transcribe(self, audio, mime, lang_hint, phrase_hints):
        failed = False
        result = None
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                with suppress_instrumentation():
                    token = await asyncio.to_thread(self.token_loader)
                    async with (
                        httpx.AsyncClient(
                            timeout=TIMEOUT_SECONDS,
                            follow_redirects=False,
                            transport=self.transport,
                        ) as client,
                        client.stream(
                            "POST",
                            self.url,
                            headers={"Authorization": f"Bearer {token}"},
                            json=request_body(audio, mime, lang_hint, phrase_hints),
                        ) as response,
                    ):
                        if response.status_code != 200:
                            raise ValueError()
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise ValueError()
                            payload.extend(chunk)
                        import json

                        result = parse_response(json.loads(payload))
        except Exception:  # noqa: BLE001 - sanitize every SDK/transport/parser failure
            # Raise outside the handler so no raw exception survives in the
            # public exception chain or an auto-recorded exception event.
            failed = True
        finally:
            audio = None
            phrase_hints = ()
        if failed:
            raise ProviderUnavailable() from None
        return result
