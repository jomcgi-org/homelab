"""Agentic runner: a model explores and edits a real repo snapshot via tools.

Unlike the single-shot runner, the model is given file tools (list/read/write) and
works over multiple turns to make the change itself, then the hidden verifier grades
the resulting workdir. Native tool-calling means file content is carried in
API-serialized JSON, so backslashes and quotes cannot corrupt it (the format noise
that dominated the single-shot contract).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from bench.cache import HARNESS_VERSION
from bench.schema import Attempt, ResultCell

MAX_READ_BYTES = 100_000
MAX_LIST_ENTRIES = 400

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories under a path (relative to the repo root).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path, '.' for root.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Return the full contents of a file (relative to the repo root).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrite a file with new complete contents (creates it if absent).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call when the change is complete and ready to be graded.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Opt-in shell tool (task.agent.exec). For tasks where the model must stand up its own
# toolchain, e.g. initialise a Go module and run its tests. Off by default so the
# file-only tasks keep their calibrated behaviour.
RUN_TOOL = {
    "type": "function",
    "function": {
        "name": "run",
        "description": (
            "Run a shell command in the repo root; returns its combined stdout+stderr "
            "and exit code. Use for toolchain setup and tests, for example "
            "`go mod init <module>`, `go mod tidy`, and `go test ./...`. Runs in a "
            "scrubbed sandbox with no cluster credentials; network access is available "
            "so dependency fetches work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run.",
                }
            },
            "required": ["command"],
        },
    },
}
RUN_TIMEOUT_S = 240  # go mod tidy + go test can be slow on the first dependency fetch.
MAX_RUN_OUTPUT = 20_000

AGENT_SYSTEM = (
    "You are a software engineer working inside a real code repository. Use the tools "
    "to explore the files, then make the change the task asks for by writing complete "
    "updated file contents with write_file. Keep changes minimal and consistent with "
    "the surrounding code. If a `run` tool is available you may execute shell commands "
    "to set up a toolchain and run the tests yourself before finishing. When the change "
    "is complete, call done."
)


def _safe_path(workdir: Path, rel: str) -> Path | None:
    """Resolve rel under workdir, rejecting escapes (path traversal / absolute)."""
    try:
        target = (workdir / rel).resolve()
        target.relative_to(workdir.resolve())
        return target
    except (ValueError, OSError):
        return None


def _execute_tool(name: str, args: dict, workdir: Path) -> str:
    if name == "done":
        return "acknowledged"
    if name == "run":
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return "error: command must be a non-empty string"
        # Reuse the verifier sandbox: scrubbed env (no creds), temp cwd, hard timeout.
        from bench.verifiers.sandbox import run_sandboxed

        res = run_sandboxed(["sh", "-c", command], cwd=workdir, timeout_s=RUN_TIMEOUT_S)
        out = (res.stdout or "") + (res.stderr or "")
        return f"exit {res.rc}\n{out[:MAX_RUN_OUTPUT]}"
    rel = args.get("path", "")
    target = _safe_path(workdir, rel)
    if target is None:
        return f"error: path {rel!r} is outside the repo"
    if name == "list_dir":
        if not target.is_dir():
            return f"error: {rel!r} is not a directory"
        entries = []
        for p in sorted(target.iterdir())[:MAX_LIST_ENTRIES]:
            entries.append(p.name + ("/" if p.is_dir() else ""))
        return "\n".join(entries) or "(empty)"
    if name == "read_file":
        if not target.is_file():
            return f"error: {rel!r} is not a file"
        data = target.read_text(errors="replace")
        return data[:MAX_READ_BYTES]
    if name == "write_file":
        content = args.get("content")
        if not isinstance(content, str):
            return "error: content must be a string"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} bytes to {rel}"
    return f"error: unknown tool {name}"


async def run_agent_cell(
    *,
    task_id: str,
    task_version: str,
    model_id: str,
    content_hash: str,
    fixture_dir: Path,
    task_prompt: str,
    chat,
    verify,
    verifier_args: dict,
    cost_fn,
    max_turns: int = 20,
    max_tokens: int = 8192,
    allow_exec: bool = False,
) -> ResultCell:
    """Run one agentic (task, model) cell and grade the resulting workdir.

    chat: async (*, model, messages, tools, temperature, max_tokens) -> ChatResult.
    verify: (workdir, args) -> VerifyResult, run once after the agent finishes.
    allow_exec: expose the sandboxed `run` shell tool (task.agent.exec).
    """
    workdir = Path(tempfile.mkdtemp())
    shutil.copytree(fixture_dir, workdir, dirs_exist_ok=True)
    tools = TOOLS + [RUN_TOOL] if allow_exec else TOOLS
    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": task_prompt},
    ]
    prompt_tokens = completion_tokens = latency_ms = 0
    turns = 0
    saw_tool_call = False  # did the model ever drive the loop with a tool call?
    saw_bad_args = False  # did any tool call carry unparseable arguments?
    try:
        for turns in range(1, max_turns + 1):
            res = await chat(
                model=model_id,
                messages=messages,
                tools=tools,
                temperature=0.0,
                max_tokens=max_tokens,
            )
            prompt_tokens += res.prompt_tokens
            completion_tokens += res.completion_tokens
            latency_ms += res.latency_ms
            msg = res.message
            messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # No tool call: the model is done or is just talking. Stop.
                break
            saw_tool_call = True
            finished = False
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    call_args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    call_args = {}
                    saw_bad_args = True
                result = _execute_tool(name, call_args, workdir)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "name": name,
                        "content": result,
                    }
                )
                if name == "done":
                    finished = True
            if finished:
                break
        r = verify(workdir, verifier_args)
        passed, feedback = r.passed, r.feedback
    except Exception as exc:  # noqa: BLE001 - a harness/tool-call error becomes a fail cell
        passed, feedback = False, f"[harness error] {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Tool-use reliability signal: a model that never emitted a valid tool call could
    # not drive the loop at all; malformed arguments (the qwen3-coder failure mode) are
    # also a reliability miss. Recorded so the leaderboard can flag flaky tool callers
    # rather than have them silently register as plain task failures.
    tool_use_ok = saw_tool_call and not saw_bad_args
    if not passed:
        if not saw_tool_call:
            feedback = f"[no tool calls emitted] {feedback}"
        elif saw_bad_args:
            feedback = f"[malformed tool-call arguments] {feedback}"

    attempt = Attempt(
        passed=passed,
        feedback=feedback if not passed else "",
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return ResultCell(
        task_id=task_id,
        task_version=task_version,
        model_id=model_id,
        content_hash=content_hash,
        outcome="pass@1" if passed else "fail",
        attempts=[attempt],
        cost_usd=cost_fn(prompt_tokens, completion_tokens),
        harness_version=HARNESS_VERSION,
        prompt_template_hash="agent",
        turns=turns,
        tool_use_ok=tool_use_ok,
    )
