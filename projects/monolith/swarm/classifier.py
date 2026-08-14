"""Fail-closed task classification through the local Qwen service."""

from __future__ import annotations

import os
import re
import time

import httpx

from chat.vision import LLAMA_CPP_URL

_CLASSIFICATION_PATTERN = re.compile(
    r"CLASSIFICATION:\s*(one_shot|planned)", re.IGNORECASE
)
_TAIL_LINES = 3
_TIMEOUT_SECONDS = 5.0
_MODEL = "qwen3.6-27b"


def _parse_classification(text: str | None) -> str | None:
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches: list[str] = []
    for line in lines[-_TAIL_LINES:]:
        stripped = line.strip("*#->` \t").rstrip(".!:").strip()
        match = _CLASSIFICATION_PATTERN.fullmatch(stripped)
        if match:
            matches.append(match.group(1).lower())
    if len(matches) != 1:
        return None
    return matches[0]


async def classify_task_with_outcome(text: str) -> tuple[str, int, str, str | None]:
    """Classify a task as one_shot or planned, returning classification plus recording metadata.

    Returns: (classification, latency_ms, outcome, refusal_code)
    - classification: "one_shot" or "planned"
    - latency_ms: round-trip time to the model
    - outcome: "success", "timeout", "error", or "unparseable"
    - refusal_code: null on success, error description on failure
    """
    started = time.monotonic()
    try:
        url = os.environ.get("LLAMA_CPP_URL", LLAMA_CPP_URL)
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": _MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Classify the task as one-shot or planned. Reply with "
                                "exactly one final line: CLASSIFICATION: one_shot or "
                                "CLASSIFICATION: planned"
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0,
                    "max_tokens": 64,
                },
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        classification = _parse_classification(content)
        if classification is None:
            return (
                "one_shot",
                _latency_ms(started),
                "unparseable",
                "unparseable response",
            )
        return classification, _latency_ms(started), "success", None
    except httpx.TimeoutException:
        return "one_shot", _latency_ms(started), "timeout", "classifier timeout"
    except Exception as exc:  # noqa: BLE001 - classification must fail closed
        return "one_shot", _latency_ms(started), "error", str(exc)


def _latency_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
