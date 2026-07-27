from __future__ import annotations

import io
import json

import pytest

from agent_sessions.transport import LocalSubprocessTransport


class FakeProcess:
    def __init__(self, lines, stdout_text=None):
        self.stdin = CaptureIO()
        self.stdout = io.StringIO(
            stdout_text or "".join(json.dumps(line) + "\n" for line in lines)
        )
        self.killed = False
        self.signals = []

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True

    def send_signal(self, signal):
        self.signals.append(signal)


class CaptureIO(io.StringIO):
    def close(self):
        pass


def test_transport_writes_one_user_jsonl_and_uses_per_turn_metrics(monkeypatch):
    process = FakeProcess(
        [
            {"type": "system", "subtype": "hook_started"},
            {"type": "system", "subtype": "hook_started"},
            {"type": "system", "subtype": "hook_response"},
            {"type": "system", "subtype": "hook_response"},
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "none",
                "session_id": "sid",
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                    ]
                },
            },
            {
                "type": "result",
                "result": "finished",
                "terminal_reason": "completed",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 9},
                "total_cost_usd": 0.12,
                "duration_ms": 44,
                "duration_api_ms": 999,
            },
        ]
    )
    monkeypatch.setattr(
        "agent_sessions.transport.subprocess.Popen", lambda *args, **kwargs: process
    )
    turn = LocalSubprocessTransport().deliver(None, "hello")
    payload = json.loads(process.stdin.getvalue())
    assert payload["message"]["content"][0]["text"] == "hello"
    assert turn.usage == {"input_tokens": 9}
    assert turn.total_cost_usd == 0.12
    assert turn.duration_ms == 44
    assert turn.activities == [{"tool": "Bash", "command": "ls"}]


def test_transport_rejects_unexpected_api_key_source(monkeypatch):
    process = FakeProcess(
        [
            {"type": "system", "subtype": "hook_started"},
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "ANTHROPIC_API_KEY",
            },
        ]
    )
    monkeypatch.setattr(
        "agent_sessions.transport.subprocess.Popen", lambda *args, **kwargs: process
    )
    with pytest.raises(ValueError, match="Unexpected apiKeySource"):
        LocalSubprocessTransport(expected_api_key_source="none").deliver(None, "hello")
    assert process.killed


def test_transport_tolerates_truncated_final_line(monkeypatch):
    process = FakeProcess(
        [],
        stdout_text=(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "apiKeySource": "none",
                    "session_id": "sid",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}]
                    },
                }
            )
            + "\n"
            + '{"type":"result","result":"partial text"'
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.transport.subprocess.Popen", lambda *args, **kwargs: process
    )

    turn = LocalSubprocessTransport().deliver(None, "hello")

    assert turn.session_id == "sid"
    assert turn.activities == [{"tool": "Bash"}]
