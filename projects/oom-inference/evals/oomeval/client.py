"""Streaming chat client for an OpenAI-compatible endpoint (standard library only)."""

import json
import time
import urllib.request
from dataclasses import dataclass


@dataclass
class Completion:
    content: str
    reasoning: str
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    ttft_s: float  # request sent to first generated token (reasoning or content)
    decode_s: float  # first to last generated token
    wall_s: float


def chat(url, body, timeout):
    """Streams one chat completion and returns its text, usage and timings."""
    body = dict(body, stream=True, stream_options={"include_usage": True})
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    content, reasoning = [], []
    finish, usage = None, {}
    start = time.perf_counter()
    first = last = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            if "error" in event:
                raise RuntimeError(event["error"])
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                piece_r = delta.get("reasoning_content")
                piece_c = delta.get("content")
                if piece_r or piece_c:
                    now = time.perf_counter()
                    first = first or now
                    last = now
                    if piece_r:
                        reasoning.append(piece_r)
                    if piece_c:
                        content.append(piece_c)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
    end = time.perf_counter()
    first = first or end
    last = last or end
    return Completion(
        content="".join(content),
        reasoning="".join(reasoning),
        finish_reason=finish,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        ttft_s=first - start,
        decode_s=last - first,
        wall_s=end - start,
    )


def wait_ready(url, timeout):
    """Polls /health until the server has loaded its model; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=10):
                return True
        except OSError:
            time.sleep(5)
    return False


def models(url, timeout=10):
    """Returns the server's /v1/models listing, or None if unavailable."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v1/models", timeout=timeout) as resp:
            return json.load(resp)
    except OSError:
        return None
