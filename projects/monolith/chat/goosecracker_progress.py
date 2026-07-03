"""In-process live-progress buffer for /goosecracker (ADR 024).

While a goosecracker run builds, the guest streams goose's stdout to
``POST /internal/goosecracker/progress`` and the Discord bot (same monolith
process) reads the per-thread buffer to edit the thread message live, so the
owner sees activity within seconds instead of a multi-minute silent wait.

In-memory and single-replica by design: this is transient build output keyed by
the Discord thread id (== the artifact id). If the pod restarts mid-build the
run is lost anyway, so the buffer needs no durability. The buffer keeps only the
tail (Discord shows ~2000 chars), so memory is bounded per thread.

ADR 035 adds a structured stage-marker channel alongside the raw text tail.
Recipes can emit marker lines on stdout to announce a plan and progress
through it: ``::stages::<n>`` declares a fresh plan of (advisory) length
``n``, and ``::stage::<index>::<state>::<title>`` reports one stage's current
state. Marker lines are parsed out of the stream and never rendered as raw
text; everything else behaves exactly as before.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

# Keep only the tail of the output: Discord renders ~2000 chars and the bot
# shows a rolling window, so retaining more wastes memory.
_MAX_BUFFER = 16384

_STAGE_MARKER_PREFIX = "::stage::"
_STAGES_MARKER_PREFIX = "::stages::"
_STAGE_STATES = frozenset({"pending", "running", "done", "failed", "skipped"})


@dataclass
class Stage:
    """One step of a recipe's announced plan, as reported by a stage marker."""

    index: int
    title: str
    state: str


@dataclass
class Progress:
    """A single run's accumulated stdout tail and completion flag.

    ``notice`` is a transient out-of-band status line (e.g. "retrying a
    transient fc-invoke failure") that the runner sets before the guest starts
    streaming, so the bot can show the owner what is happening during a retry
    instead of a bare "Thinking". It is cleared as soon as real stdout flows.

    ``stages`` is the structured plan reported via stage markers (see module
    docstring), ordered by arrival. ``stages_version`` bumps on any change to
    ``stages`` so the renderer can cheaply detect "needs edit" without diffing
    the list itself.
    """

    text: str = ""
    done: bool = False
    notice: str = ""
    stages: list[Stage] = field(default_factory=list)
    stages_version: int = 0
    updated_at: float = field(default_factory=time.monotonic)


_store: dict[str, Progress] = {}
_lock = Lock()

# Per-artifact unterminated trailing line fragment, carried forward until the
# next chunk completes it. Chunks are not guaranteed to be line-aligned, so a
# marker (or the plain text before one) can be split across two append()
# calls; this is what lets us reassemble it before deciding what it is.
_partial: dict[str, str] = {}


def _looks_like_marker_prefix(fragment: str) -> bool:
    """True if an unterminated line could still grow into a marker line.

    Used to decide whether to hold an incomplete trailing fragment back from
    ``text`` (it might be a marker) or flush it immediately (it can't be).
    """
    if not fragment:
        return False
    for prefix in (_STAGE_MARKER_PREFIX, _STAGES_MARKER_PREFIX):
        if prefix.startswith(fragment) or fragment.startswith(prefix):
            return True
    return False


def _apply_marker(p: Progress, line: str) -> None:
    """Parse one complete marker line and mutate ``p.stages`` in place.

    Malformed markers (unknown state, non-integer index, wrong shape) are
    dropped silently: no stage is added or updated, and the line is not
    rendered as text either, since it was already routed away from ``text``
    by prefix.
    """
    if line.startswith(_STAGES_MARKER_PREFIX):
        # "::stages::<n>": a fresh plan announcement (e.g. after steering
        # triggers re-planning). <n> is advisory only, the stage markers that
        # follow populate the list, so we don't validate or pre-size on it.
        p.stages = []
        p.stages_version += 1
        return

    # "::stage::<index>::<state>::<title>". Split with maxsplit=4 so a title
    # containing "::" is not mangled: parts are ("", "stage", index, state,
    # title).
    parts = line.split("::", 4)
    if len(parts) != 5:
        return
    _, _tag, index_str, state, title = parts
    if state not in _STAGE_STATES:
        return
    try:
        index = int(index_str)
    except ValueError:
        return

    for stage in p.stages:
        if stage.index == index:
            if stage.title == title and stage.state == state:
                return
            stage.title = title
            stage.state = state
            p.stages_version += 1
            return
    p.stages.append(Stage(index=index, title=title, state=state))
    p.stages_version += 1


def append(artifact_id: str, chunk: str) -> None:
    """Append a stdout chunk to a run's buffer, keeping only the tail.

    Marker lines (see module docstring) are parsed into ``Progress.stages``
    and never appear in ``Progress.text``. Everything else is appended
    exactly as before, including chunks with no trailing newline.
    """
    if not chunk:
        return
    with _lock:
        p = _store.setdefault(artifact_id, Progress())
        pending = _partial.pop(artifact_id, "")
        combined = pending + chunk
        segments = combined.split("\n")
        incomplete = segments[-1]
        complete_lines = segments[:-1]

        # Segments we keep for text, in order. Complete lines that are
        # markers are dropped (parsed instead); everything else is kept.
        # Rejoining the kept segments with "\n" reproduces the original
        # chunk minus exactly the marker lines and their newlines.
        text_segments: list[str] = []
        for line in complete_lines:
            stripped = line.strip()
            if stripped.startswith(_STAGE_MARKER_PREFIX) or stripped.startswith(
                _STAGES_MARKER_PREFIX
            ):
                _apply_marker(p, stripped)
            else:
                text_segments.append(line)

        if _looks_like_marker_prefix(incomplete):
            # Might still grow into a marker: hold it back until the next
            # chunk completes the line (or proves it isn't one).
            _partial[artifact_id] = incomplete
        else:
            # Not a marker in progress, so flush it immediately: plain
            # chunks with no newline must append exactly as before.
            text_segments.append(incomplete)

        if text_segments:
            p.text = (p.text + "\n".join(text_segments))[-_MAX_BUFFER:]

        # Real output supersedes any pre-run retry notice: once the guest is
        # streaming, the retry is over, so drop the stale line automatically.
        p.notice = ""
        p.updated_at = time.monotonic()


def set_notice(artifact_id: str, notice: str) -> None:
    """Set a transient out-of-band status line for a run (see Progress.notice).

    Used by the runner to tell the owner that a transient fc-invoke failure is
    being retried, before the guest starts streaming its own stdout.
    """
    with _lock:
        p = _store.setdefault(artifact_id, Progress())
        p.notice = notice
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
        return Progress(
            text=p.text,
            done=p.done,
            notice=p.notice,
            stages=[
                Stage(index=s.index, title=s.title, state=s.state) for s in p.stages
            ],
            stages_version=p.stages_version,
            updated_at=p.updated_at,
        )


def clear(artifact_id: str) -> None:
    """Drop a run's buffer once the bot has finished streaming it."""
    with _lock:
        _store.pop(artifact_id, None)
        _partial.pop(artifact_id, None)
