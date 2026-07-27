from __future__ import annotations

import re

_VOICE_RE = re.compile(r"<voice>(.*?)</voice>", re.IGNORECASE | re.DOTALL)


def extract_voice_summary(result_text: str) -> str:
    if not result_text:
        return ""
    match = _VOICE_RE.search(result_text)
    if match:
        return " ".join(match.group(1).split()).strip()[:200]
    first_sentence = re.split(r"(?<=[.!?])\s+", result_text.strip(), maxsplit=1)[0]
    return " ".join(first_sentence.split())[:200]


def qwen_voice_fallback(result_text: str) -> str:
    """Reserved hook for a future Qwen summary fallback."""
    return ""
