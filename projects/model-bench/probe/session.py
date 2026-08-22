"""HTTP client and polling for monolith agent sessions."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx


@dataclass
class PollOutcome:
    turn: dict | None
    wall_s: float
    timed_out: bool

    @property
    def grade(self) -> str:
        return "timeout" if self.timed_out else "completed"


class AgentSessionClient:
    """Small typed facade over the monolith session and compare endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def start(self, payload: dict) -> dict:
        response = self._client.post(
            "/api/agents/sessions",
            json=payload,
            headers={"x-auth-email": "probe@model-bench"},
        )
        response.raise_for_status()
        return _object(response)

    def detail(self, session_id: int) -> dict:
        response = self._client.get(f"/api/agents/sessions/{session_id}")
        response.raise_for_status()
        return _object(response)

    def delete(self, session_id: int) -> None:
        response = self._client.delete(f"/api/agents/sessions/{session_id}")
        response.raise_for_status()

    def compare(self, session_id: int, turn: int) -> dict:
        response = self._client.get(f"/api/swarm/compare/{session_id}/{turn}")
        response.raise_for_status()
        return _object(response)

    def patch(self, session_id: int, turn: int, path: str) -> dict:
        response = self._client.get(
            f"/api/swarm/compare/{session_id}/{turn}/patch", params={"path": path}
        )
        response.raise_for_status()
        return _object(response)


def _object(response: httpx.Response) -> dict:
    data = response.json()
    if not isinstance(data, dict):
        raise TypeError("monolith returned a non-object JSON response")
    return data


def poll_turn(
    client: AgentSessionClient,
    session_id: int,
    turn_seq: int,
    *,
    timeout_s: float,
    started_monotonic: float,
    poll_interval_s: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PollOutcome:
    """Poll until the persisted terminal turn appears or the deadline passes."""
    deadline = started_monotonic + timeout_s
    while True:
        detail = client.detail(session_id)
        turns = detail.get("turns", [])
        if isinstance(turns, list):
            for turn in turns:
                if isinstance(turn, dict) and turn.get("seq") == turn_seq:
                    return PollOutcome(turn, clock() - started_monotonic, False)
        now = clock()
        if now >= deadline:
            return PollOutcome(None, now - started_monotonic, True)
        sleep(min(poll_interval_s, deadline - now))


def unified_diff(files: list[dict], patches: dict[str, str | None]) -> str:
    """Restore git headers around the hunk-only compare API patches."""
    blocks: list[str] = []
    for item in files:
        path = str(item.get("path", ""))
        patch = patches.get(path)
        if not path or patch is None:
            continue
        status = item.get("status")
        header = [f"diff --git a/{path} b/{path}\n"]
        if status == "added":
            header.extend(
                ["new file mode 100644\n", "--- /dev/null\n", f"+++ b/{path}\n"]
            )
        elif status in {"removed", "deleted"}:
            header.extend(
                ["deleted file mode 100644\n", f"--- a/{path}\n", "+++ /dev/null\n"]
            )
        else:
            header.extend([f"--- a/{path}\n", f"+++ b/{path}\n"])
        blocks.append("".join(header) + patch.rstrip("\n") + "\n")
    return "".join(blocks)
