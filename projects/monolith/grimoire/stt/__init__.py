"""Swappable short-segment recognition. Importing this module reads no credentials."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Segment:
    text: str
    confidence: float | None = None
    # Only the provider's response can confirm billing. None means unknown.
    billed_seconds: float | None = None


class Transcriber(Protocol):
    async def transcribe(
        self, audio: bytes, mime: str, lang_hint: str, phrase_hints: tuple[str, ...]
    ) -> Segment: ...


class ProviderUnavailable(Exception):
    """A deliberately content-free provider failure."""


class FakeTranscriber:
    """Deterministic offline seam; it never retains the input."""

    async def transcribe(self, audio, mime, lang_hint, phrase_hints) -> Segment:
        return Segment("A spoken clue", 0.9, 1.0)
