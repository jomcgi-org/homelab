from __future__ import annotations

import json
import logging
import queue
import signal
import subprocess
import threading
import time
from typing import NamedTuple, Protocol


logger = logging.getLogger(__name__)


class Turn(NamedTuple):
    result: str
    terminal_reason: str | None
    stop_reason: str | None
    is_error: bool
    permission_denials: list
    num_turns: int
    session_id: str | None
    usage: dict
    total_cost_usd: float | None
    duration_ms: int | None
    activities: list[dict]


class ShimTransport(Protocol):
    def deliver(self, session_id: str | None, message: str) -> Turn: ...


class LocalSubprocessTransport:
    overall_timeout_seconds = 30
    shutdown_grace_seconds = 5

    def __init__(
        self,
        expected_api_key_source: str = "none",
        voice_prompt: str = "Respond with a concise human-readable summary in <voice>...</voice>.",
        permission_mode: str = "default",
    ) -> None:
        self.expected_api_key_source = expected_api_key_source
        self.voice_prompt = voice_prompt
        # Use "default" mode by default: this transport spawns the CLI in the
        # monolith pod (the orchestrator), not a sandbox. Unlike the EmberVM guest
        # (which uses "bypassPermissions" since the VM is the security boundary),
        # the monolith pod is not isolated. An agent with "bypassPermissions" would
        # have unrestricted access to the session store, notify path, and MCP
        # surface, a materially larger blast radius. Callers must choose explicitly.
        self.permission_mode = permission_mode

    def deliver(self, session_id: str | None, message: str) -> Turn:
        command = [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            self.permission_mode,
        ]
        if session_id:
            command.extend(["--resume", session_id])
        if self.voice_prompt:
            command.extend(["--append-system-prompt", self.voice_prompt])
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": message}],
                    },
                }
            )
            + "\n"
        )
        process.stdin.close()
        # Deliberate: close stdin to signal end-of-input and let the process exit
        # after this turn. This is the local dev transport; it prioritizes simplicity.
        # The in-guest shim (projects/embervm/runtimes/claude/shim.py) reuses the
        # process across turns to amortize the ~250ms spawn cost over many turns.
        assert process.stdout is not None
        activities: list[dict] = []
        local_session_id = None
        result_event: dict = {}
        saw_init = False
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    output_queue.put(line)
            except Exception:
                pass
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + self.overall_timeout_seconds
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = output_queue.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if line is None:
                break
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A CLI interrupted mid-write can leave a truncated JSONL line.
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "system" and event.get("subtype") == "init":
                saw_init = True
                local_session_id = event.get("session_id")
                api_key_source = event.get("apiKeySource")
                if api_key_source != self.expected_api_key_source:
                    process.kill()
                    raise ValueError(
                        f"Unexpected apiKeySource {api_key_source!r}, "
                        f"expected {self.expected_api_key_source!r}"
                    )
            elif event_type == "assistant":
                message_data = event.get("message", event)
                for block in message_data.get("content", []):
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    activity = {"tool": name}
                    if name == "Bash" and "command" in tool_input:
                        activity["command"] = tool_input["command"]
                    elif name in {"Edit", "Write"} and "file_path" in tool_input:
                        activity["file_path"] = tool_input["file_path"]
                    activities.append(activity)
            elif event_type == "result":
                result_event = event

        if timed_out:
            logger.warning("Claude turn timed out; sending SIGINT")
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=self.shutdown_grace_seconds)
            except subprocess.TimeoutExpired:
                logger.warning("Claude did not exit after SIGINT; sending SIGKILL")
                process.kill()
                process.wait()
            raise RuntimeError("Turn timed out; sent SIGINT, then SIGKILL")

        # The reader consumed all remaining output, including output after result.
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if not saw_init:
            raise ValueError("Claude stream did not emit a system init event")
        return Turn(
            result=result_event.get("result", ""),
            terminal_reason=result_event.get("terminal_reason"),
            stop_reason=result_event.get("stop_reason"),
            is_error=bool(result_event.get("is_error", False)),
            permission_denials=result_event.get("permission_denials") or [],
            num_turns=int(result_event.get("num_turns", 0)),
            session_id=result_event.get("session_id") or local_session_id,
            usage=result_event.get("usage") or {},
            total_cost_usd=result_event.get("total_cost_usd"),
            duration_ms=result_event.get("duration_ms"),
            activities=activities,
        )
