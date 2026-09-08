"""Small in-process admission controls for public knowledge endpoints."""

from __future__ import annotations

import threading
from collections import deque
from hashlib import sha256
from time import monotonic

SEMANTIC_SEARCH_LIMIT = 10
SEMANTIC_SEARCH_WINDOW_SECONDS = 60.0

_lock = threading.Lock()
_requests: dict[str, deque[float]] = {}


def allow_semantic_search(client: str, *, now: float | None = None) -> bool:
    """Allow at most ten semantic searches per client in a rolling minute."""
    current = monotonic() if now is None else now
    cutoff = current - SEMANTIC_SEARCH_WINDOW_SECONDS
    client_tag = sha256(client.encode()).hexdigest()[:16]
    with _lock:
        stale = [
            key
            for key, values in _requests.items()
            if not values or values[-1] <= cutoff
        ]
        for key in stale:
            _requests.pop(key, None)
        if client_tag not in _requests and len(_requests) >= 4096:
            return False

        timestamps = _requests.setdefault(client_tag, deque())
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= SEMANTIC_SEARCH_LIMIT:
            return False
        timestamps.append(current)

        return True


def reset_semantic_search_limits() -> None:
    """Clear process-local state for hermetic tests."""
    with _lock:
        _requests.clear()
