"""Claude Code subprocess backend for frontier anchors and the judge.

Anchors (Claude Opus/Sonnet) and the LLM judge run through the local `claude` CLI
under the Max subscription, not OpenRouter. Renting Claude via OpenRouter to produce a
"cloud cost" row is money that would never actually be spent (Claude is consumed via the
flat-rate subscription), so that cost is both expensive to measure and unrepresentative.
The `claude` CLI (a subprocess, not the SDK) is the ToS-compliant path under Max.

An anchor is a CEILING reference, not a ranked competitor. It runs inside Claude Code's
own agent harness (its own tools and loop), not the bench's OpenRouter tool loop, so its
turns and tokens are NOT directly comparable to candidate rows. We record pass/fail plus
wall-time; OpenRouter cost is not applicable and is reported as 0.

The single subprocess entry-point (`_invoke`) is module-level so tests can monkeypatch
it without spawning a real `claude` process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from bench.cache import HARNESS_VERSION
from bench.schema import Attempt, ResultCell

# Override for tests / non-standard installs. Default resolves `claude` on PATH.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Tools an anchor may use to edit the workdir. Read/search + the three mutators; no
# network or MCP, so a task is solved from the fixture tree alone, like the candidates.
_ANCHOR_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]

# Generous ceiling: an agentic anchor run can legitimately take minutes. A candidate's
# per-cell wall-time is bounded by max_turns * per-call latency; anchors get a flat cap.
_DEFAULT_TIMEOUT_S = 900


@dataclass
class ClaudeResult:
    """Parsed `claude -p --output-format json` result."""

    text: str
    num_turns: int
    is_error: bool
    wall_ms: int


def _invoke(
    prompt: str,
    *,
    cwd: Path | None = None,
    allowed_tools: list[str] | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> ClaudeResult:
    """Run `claude -p` once and parse its JSON result.

    When cwd is set, the run is allowed to edit files there (`--permission-mode
    acceptEdits`); otherwise it is a plain text completion (the judge, single-shot).
    Wall-time is measured here rather than trusting the CLI's own duration field so the
    number is comparable to the OpenRouter latency the candidates record.
    """
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    if cwd is not None:
        cmd += ["--permission-mode", "acceptEdits"]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    wall_ms = int((time.monotonic() - t0) * 1000)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"claude CLI returned non-object JSON: {type(data).__name__}"
        )
    return ClaudeResult(
        text=str(data.get("result", "")),
        num_turns=int(data.get("num_turns", 0)),
        is_error=bool(data.get("is_error", False)),
        wall_ms=wall_ms,
    )


def judge_caller(prompt: str) -> str:
    """Synchronous judge backend: one `claude -p` text completion, no tools.

    Drop-in for judge_free_text's Callable[[str], str]. Free under Max, so the judge is
    no longer an OpenRouter cost and the self-preference guard is moot (Claude is never a
    candidate any more).
    """
    return _invoke(prompt).text


@dataclass
class AnchorCompletion:
    """OpenRouter ChatResult-shaped object for run_cell's single-shot `complete`."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


def _flatten_messages(messages: list[dict]) -> str:
    """Collapse a chat `messages` list into one self-contained prompt.

    `claude -p` spawns a fresh, STATELESS subprocess each call: there is no conversation
    to carry forward, so run_cell's shot-2 retry (which passes [task, prior assistant
    reply, corrective note]) must be flattened into a single prompt or the anchor would
    see only "fix it" with no task and no prior attempt. A single-message shot-1 passes
    through unchanged.
    """
    if len(messages) <= 1:
        return messages[0].get("content", "") if messages else ""
    parts: list[str] = []
    for m in messages:
        content = m.get("content", "")
        if m.get("role") == "assistant":
            parts.append(f"--- Your previous attempt ---\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


async def complete(**kwargs) -> AnchorCompletion:
    """Single-shot completion backend for run_cell, backed by the `claude` CLI.

    Accepts run_cell's (model, messages, temperature, max_tokens) kwargs. The full
    `messages` list is flattened into one self-contained prompt (the CLI is stateless),
    so a shot-2 retry still carries the original task and the prior attempt. Tokens are 0
    (not metered here); cost is 0.
    """
    prompt = _flatten_messages(kwargs.get("messages") or [])
    res = _invoke(prompt)
    return AnchorCompletion(
        text=res.text, prompt_tokens=0, completion_tokens=0, latency_ms=res.wall_ms
    )


def run_anchor_agent_cell(
    *,
    task_id: str,
    task_version: str,
    model_id: str,
    content_hash: str,
    fixture_dir: Path,
    task_prompt: str,
    verify,
    verifier_args: dict,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> ResultCell:
    """Run one agentic anchor cell: hand the whole task to `claude -p` in a workdir copy,
    let it edit files with its own tools, then grade the workdir with the same verifier.

    This is deliberately NOT the bench's OpenRouter tool loop: the anchor uses Claude
    Code's native harness. Turns come from the CLI (`num_turns`); wall-time is measured;
    cost is 0 (free under Max). tool_use_ok is True because Claude Code drives its own
    reliable tool loop, so the candidate flaky-tool-caller signal does not apply.
    """
    workdir = Path(tempfile.mkdtemp())
    shutil.copytree(fixture_dir, workdir, dirs_exist_ok=True)
    turns = 0
    wall_ms = 0
    try:
        res = _invoke(
            task_prompt,
            cwd=workdir,
            allowed_tools=_ANCHOR_TOOLS,
            timeout_s=timeout_s,
        )
        turns = res.num_turns
        wall_ms = res.wall_ms
        if res.is_error:
            passed, feedback = False, "[claude CLI reported is_error] " + res.text[:500]
        else:
            r = verify(workdir, verifier_args)
            passed, feedback = r.passed, r.feedback
    except Exception as exc:  # noqa: BLE001 - a subprocess/verify error becomes a fail cell
        passed, feedback = False, f"[anchor harness error] {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    attempt = Attempt(
        passed=passed,
        feedback=feedback if not passed else "",
        latency_ms=wall_ms,
        prompt_tokens=0,
        completion_tokens=0,
    )
    return ResultCell(
        task_id=task_id,
        task_version=task_version,
        model_id=model_id,
        content_hash=content_hash,
        outcome="pass@1" if passed else "fail",
        attempts=[attempt],
        cost_usd=0.0,
        harness_version=HARNESS_VERSION,
        prompt_template_hash="agent",
        turns=turns,
        tool_use_ok=True,
    )
