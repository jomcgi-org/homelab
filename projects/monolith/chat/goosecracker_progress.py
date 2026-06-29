"""In-process live-progress buffer for /goosecracker (ADR 024).

While a goosecracker run builds, the guest streams goose's stdout to
``POST /internal/goosecracker/progress`` and the Discord bot (same monolith
process) reads the per-thread buffer to edit the thread message live, so the
owner sees activity within seconds instead of a multi-minute silent wait.

In-memory and single-replica by design: this is transient build output keyed by
the Discord thread id (== the artifact id). If the pod restarts mid-build the
run is lost anyway, so the buffer needs no durability. The buffer keeps only the
tail (Discord shows ~2000 chars), so memory is bounded per thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

# Keep only the tail of the output: Discord renders ~2000 chars and the bot
# shows a rolling window, so retaining more wastes memory.
_MAX_BUFFER = 16384


@dataclass
class Progress:
    """A single run's accumulated stdout tail and completion flag."""

    text: str = ""
    done: bool = False
    updated_at: float = field(default_factory=time.monotonic)


_store: dict[str, Progress] = {}
_lock = Lock()


def append(artifact_id: str, chunk: str) -> None:
    """Append a stdout chunk to a run's buffer, keeping only the tail."""
    if not chunk:
        return
    with _lock:
        p = _store.setdefault(artifact_id, Progress())
        p.text = (p.text + chunk)[-_MAX_BUFFER:]
        p.updated_at = time.monotonic()


def mark_done(artifact_id: str) -> None:
    """Mark a run complete so the bot's streamer stops editing and finishes."""
    with _lock:
        p = _store.setdefault(artifact_id, Progress())
        p.done = True
        p.updated_at = time.monotonic()


def get(artifact_id: str) -> Progress | None:
    """Return a snapshot copy of a run's progress, or None if unknown."""
    with _lock:
        p = _store.get(artifact_id)
        if p is None:
            return None
        return Progress(text=p.text, done=p.done, updated_at=p.updated_at)


def clear(artifact_id: str) -> None:
    """Drop a run's buffer once the bot has finished streaming it."""
    with _lock:
        _store.pop(artifact_id, None)
