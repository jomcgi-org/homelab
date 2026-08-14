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
# The alias resolves to a REASONING model since the llama.cpp switch
# (9697abc47). Reasoning tokens count against the generation budget even
# though --reasoning-format deepseek routes them into `reasoning_content`,
# so a budget sized for a one-line answer is spent thinking and `content`
# comes back empty. That parses to None, which fails closed to one_shot, so
# EVERY task became a session and no run could ever start.
#
# Two changes rather than one, deliberately. `enable_thinking: false` is the
# real fix (a binary classification does not need deliberation, and skipping
# it also makes the call fast), and the wider budget is the belt: if a future
# model or template ignores the flag, the answer still fits instead of
# silently reverting the entrypoint to sessions-only.
_TIMEOUT_SECONDS = 20.0
_MAX_TOKENS = 512
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
                    "max_tokens": _MAX_TOKENS,
                    # Skip deliberation for a binary label. Qwen reads this
                    # through the jinja chat template; a server that does not
                    # understand it ignores it, which is why _MAX_TOKENS still
                    # has to be large enough to hold reasoning plus answer.
                    "chat_template_kwargs": {"enable_thinking": False},
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
