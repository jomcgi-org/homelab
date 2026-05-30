"""Tests for the Claude-backed gap classifier subprocess wrapper."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from knowledge.profile import RELEVANCE_EMPLOYER_CARVE_OUTS
from knowledge.gap_classifier import (
    _CLASSIFIER_PROMPT,
    _RELEVANCE_KEEP_TEXT,
    _RELEVANCE_SKIP_TEXT,
    CLASSIFIER_VERSION,
    ClassifyStats,
    classify_stubs,
)


class _FakeProcess:
    """Minimal asyncio.subprocess.Process stand-in for tests."""

    def __init__(
        self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
    ):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover — only called on timeout
        pass

    async def wait(self) -> None:  # pragma: no cover
        return


@pytest.mark.asyncio
async def test_classify_stubs_invokes_claude_with_correct_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured args prove: Read+Edit only, no Write/Bash, prompt lists stubs."""
    captured_args: list[str] = []
    captured_kwargs: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured_args.extend(args)
        captured_kwargs.update(kwargs)
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    stubs = [tmp_path / "a.md", tmp_path / "b.md", tmp_path / "c.md"]
    stats = await classify_stubs(stubs, claude_bin="claude")

    assert stats == ClassifyStats(stubs_processed=3, duration_ms=stats.duration_ms)

    # claude binary + --print + --dangerously-skip-permissions + --allowedTools +
    # "Read,Edit" + -p + prompt
    assert captured_args[0] == "claude"
    assert "--allowedTools" in captured_args
    allowed_tools_idx = captured_args.index("--allowedTools")
    assert captured_args[allowed_tools_idx + 1] == "Read,Edit"

    # Prompt (the arg after -p) contains each stub path on its own bulleted line
    p_idx = captured_args.index("-p")
    prompt = captured_args[p_idx + 1]
    for stub in stubs:
        assert f"- {stub}" in prompt

    # Classifier version is interpolated into the prompt
    assert CLASSIFIER_VERSION in prompt

    # The 4-class rubric must be present — insurance against accidental
    # prompt rot that would silently invalidate classifications.
    for cls in ("external", "internal", "hybrid", "parked"):
        assert cls in prompt, f"prompt missing class: {cls}"

    # HOME override protecting claude's ~/.claude write
    assert captured_kwargs["env"]["HOME"] == "/tmp"


@pytest.mark.asyncio
async def test_classify_stubs_handles_subprocess_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess timeout returns ClassifyStats without raising."""

    kill_called: list[bool] = []

    async def fake_spawn(*args, **kwargs):
        process = _FakeProcess()

        async def slow_communicate():
            await asyncio.sleep(10)  # longer than our patched timeout
            return (b"", b"")

        def kill():
            kill_called.append(True)

        process.communicate = slow_communicate  # type: ignore[method-assign]
        process.kill = kill  # type: ignore[method-assign]
        return process

    # Patch the module's timeout constant to something we can actually wait out.
    monkeypatch.setattr("knowledge.gap_classifier._CLASSIFY_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    stats = await classify_stubs([tmp_path / "a.md"], claude_bin="claude")
    assert stats.stubs_processed == 1
    assert stats.duration_ms >= 0
    assert kill_called == [True]


@pytest.mark.asyncio
async def test_classify_stubs_logs_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-zero subprocess exit logs a warning with stderr excerpt."""

    async def fake_spawn(*args, **kwargs):
        return _FakeProcess(returncode=1, stderr=b"auth: invalid token")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    with caplog.at_level(logging.WARNING, logger="knowledge.gap_classifier"):
        stats = await classify_stubs([tmp_path / "a.md"], claude_bin="claude")

    assert stats.stubs_processed == 1
    assert "exit=1" in caplog.text
    assert "invalid token" in caplog.text


@pytest.mark.asyncio
async def test_classify_stubs_empty_batch_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty stub list returns zero stats without spawning anything."""
    spawned: list[bool] = []

    async def fake_spawn(*args, **kwargs):
        spawned.append(True)
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    stats = await classify_stubs([], claude_bin="claude")
    assert stats == ClassifyStats(stubs_processed=0, duration_ms=0)
    assert spawned == []


@pytest.mark.asyncio
async def test_classify_stubs_rejects_relative_paths(tmp_path: Path) -> None:
    """Relative stub paths raise a ValueError before any subprocess work."""
    with pytest.raises(ValueError, match="requires absolute paths"):
        await classify_stubs([Path("relative.md")], claude_bin="claude")


def test_classifier_prompt_explicitly_forbids_appending_duplicate_keys():
    """Drift detector: prompt must instruct find-and-replace, not append."""
    # Use phrase tokens, not exact wording — prompt iterations are expected,
    # but the substantive instruction must remain.
    assert "replace" in _CLASSIFIER_PROMPT.lower(), (
        "prompt must mention 'replace' to instruct find-and-replace edits"
    )
    assert (
        "do not add a new" in _CLASSIFIER_PROMPT.lower()
        or "do not append" in _CLASSIFIER_PROMPT.lower()
    ), "prompt must explicitly forbid appending new keys when one exists"
    # YAML uniqueness justification — keeps the rule explainable to future readers.
    assert (
        "duplicate" in _CLASSIFIER_PROMPT.lower()
        or "yaml" in _CLASSIFIER_PROMPT.lower()
    ), "prompt should explain WHY (YAML key uniqueness)"

    # Sanity: ensure the .format() placeholders are still intact and the prompt
    # still substitutes cleanly. Catches stray `{` / `}` accidentally introduced
    # to the prompt body.
    from knowledge.gap_classifier import (
        _RELEVANCE_KEEP_TEXT,
        _RELEVANCE_SKIP_TEXT,
    )
    from knowledge.profile import RELEVANCE_EMPLOYER_CARVE_OUTS

    rendered = _CLASSIFIER_PROMPT.format(
        classifier_version=CLASSIFIER_VERSION,
        stub_list="- /tmp/example.md",
        relevance_keep=_RELEVANCE_KEEP_TEXT,
        relevance_skip=_RELEVANCE_SKIP_TEXT,
        carve_outs=RELEVANCE_EMPLOYER_CARVE_OUTS,
    )
    assert CLASSIFIER_VERSION in rendered
    assert "/tmp/example.md" in rendered


def test_classifier_prompt_inlines_relevance_rubric():
    """Drift detector for the profile.py -> classifier prompt wiring.

    PR #2378 promoted Joe's relevance rubric to typed Python constants;
    this test pins that those constants actually reach the classifier
    prompt rendered at runtime. If anyone refactors the .format() call
    site without passing the rubric, the prompt would silently revert
    to the v3 four-class-only shape and SKIP-category gaps would once
    again leak through as external.
    """
    from knowledge.gap_classifier import (
        _RELEVANCE_KEEP_TEXT,
        _RELEVANCE_SKIP_TEXT,
    )
    from knowledge.profile import (
        RELEVANCE_EMPLOYER_CARVE_OUTS,
        RELEVANCE_KEEP,
        RELEVANCE_SKIP,
    )

    rendered = _CLASSIFIER_PROMPT.format(
        classifier_version=CLASSIFIER_VERSION,
        stub_list="- /tmp/example.md",
        relevance_keep=_RELEVANCE_KEEP_TEXT,
        relevance_skip=_RELEVANCE_SKIP_TEXT,
        carve_outs=RELEVANCE_EMPLOYER_CARVE_OUTS,
    )

    # A sample of KEEP domains and SKIP categories should appear verbatim.
    assert RELEVANCE_KEEP[0]["domain"] in rendered, (
        "first KEEP domain missing from rendered prompt"
    )
    assert RELEVANCE_SKIP[0]["category"] in rendered, (
        "first SKIP category missing from rendered prompt"
    )
    assert RELEVANCE_EMPLOYER_CARVE_OUTS in rendered, (
        "carve-out paragraph missing from rendered prompt"
    )
    # The explanatory framing that ties the rubric to the four-class decision
    # must survive prompt edits -- this is the load-bearing instruction.
    assert "matches SKIP" in rendered or "matches a SKIP" in rendered, (
        "prompt must instruct that SKIP-matching terms get parked"
    )


def test_classifier_prompt_routes_internal_hybrid_external_to_in_review():
    """Drift detector: internal/hybrid/external must transition to in_review.

    External moved into in_review in CLASSIFIER_VERSION opus-4-7@v2 to
    gate Sonnet web research behind explicit user approval — the review
    queue is the approval surface. Only `parked` should still route to
    status: classified (a terminal label for parked gaps that bypass the
    queue).

    Without this test, regressing the routing for any of the three
    user-actionable classes silently empties the pending review queue
    for that class and (for external) re-enables the unguarded research
    drain that v2 was introduced to stop.
    """
    rendered = _CLASSIFIER_PROMPT.format(
        classifier_version=CLASSIFIER_VERSION,
        stub_list="- /tmp/example.md",
        relevance_keep=_RELEVANCE_KEEP_TEXT,
        relevance_skip=_RELEVANCE_SKIP_TEXT,
        carve_outs=RELEVANCE_EMPLOYER_CARVE_OUTS,
    )

    # Both terminal-ish statuses must be reachable from the prompt:
    # `in_review` (the user-actionable lane) and `classified` (the
    # parked-only escape hatch).
    assert "status: in_review" in rendered, (
        "prompt must produce status: in_review for external/internal/hybrid"
    )
    assert "status: classified" in rendered, (
        "prompt must still produce status: classified for parked"
    )

    # The in_review branch must explicitly name all three user-actionable
    # classes within a tight window so the model can't reasonably misroute.
    # 240 chars covers the bullet line plus its parenthetical.
    after_in_review = rendered.split("status: in_review", 1)[1][:240]
    for cls in ("external", "internal", "hybrid"):
        assert cls in after_in_review, (
            f"in_review branch must name the {cls} class explicitly"
        )

    # The classified branch must name `parked` and must NOT name external
    # (regression guard against the v1 routing reappearing).
    after_classified = rendered.split("status: classified", 1)[1][:200]
    assert "parked" in after_classified, "classified branch must name parked explicitly"
    assert "external" not in after_classified, (
        "classified branch must NOT mention external — v2 routes external "
        "through in_review for approval gating"
    )


def test_classifier_prompt_includes_person_public_peer_subclassification():
    """v3 adds person:public vs person:peer sub-tag distinction.

    Without this rubric in the prompt, person-atoms get over-flagged
    as private because the bare `person` tag is treated as a privacy
    signal — Pierre-Simon Laplace ends up bucketed with actual peers
    when his atom should default visibility: public. See 2026-05-28
    vault audit for the over-flagging it caused.
    """
    rendered = _CLASSIFIER_PROMPT.format(
        classifier_version=CLASSIFIER_VERSION,
        stub_list="- /tmp/example.md",
        relevance_keep=_RELEVANCE_KEEP_TEXT,
        relevance_skip=_RELEVANCE_SKIP_TEXT,
        carve_outs=RELEVANCE_EMPLOYER_CARVE_OUTS,
    )
    assert "person:public" in rendered, "v3 must teach the person:public sub-tag"
    assert "person:peer" in rendered, "v3 must teach the person:peer sub-tag"
    assert "Daniel Kahneman" in rendered or "Bertrand Russell" in rendered, (
        "public-figure examples must be present so the rubric is concrete "
        "for the LLM (abstract definitions alone get misapplied)"
    )
    assert CLASSIFIER_VERSION == "opus-4-7@v3", (
        "version bump propagates into stub frontmatter so future-Joe can "
        "query for which classifier produced which atom"
    )
