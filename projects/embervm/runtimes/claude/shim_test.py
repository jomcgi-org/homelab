"""Unit tests for the Claude guest shim using a fake stream-json CLI."""

import ast
import base64
import datetime
import io
import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
import subprocess
import runpy
import zlib

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import shim


EGRESS_CA_TRUST_ENV_KEYS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "NODE_EXTRA_CA_CERTS",
)


FAKE_CLI = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys

args_path = os.environ.get("FAKE_ARGS")
if args_path:
    with open(args_path, "w") as stream:
        json.dump(sys.argv[1:], stream)
egress_env_path = os.environ.get("FAKE_EGRESS_ENV")
if egress_env_path:
    with open(egress_env_path, "w") as stream:
        json.dump(
            {
                key: os.environ.get(key)
                for key in (
                    "HTTPS_PROXY",
                    "HTTP_PROXY",
                    "NO_PROXY",
                    "https_proxy",
                    "http_proxy",
                    "no_proxy",
                )
            },
            stream,
        )
trust_env_path = os.environ.get("FAKE_TRUST_ENV")
if trust_env_path:
    with open(trust_env_path, "w") as stream:
        json.dump(
            {
                key: os.environ.get(key)
                for key in (
                    "SSL_CERT_FILE",
                    "REQUESTS_CA_BUNDLE",
                    "CURL_CA_BUNDLE",
                    "GIT_SSL_CAINFO",
                    "NODE_EXTRA_CA_CERTS",
                )
            },
            stream,
        )
api_key = os.environ.get("FAKE_API_KEY", "none")
initialized = False

def interrupted(_signum, _frame):
    sys.exit(0)

signal.signal(signal.SIGINT, interrupted)
for line in sys.stdin:
    request = json.loads(line)
    text = request["message"]["content"][0]["text"]
    if not initialized:
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "init-sid",
                          "model": "fake", "apiKeySource": api_key,
                          "mcp_servers": []}), flush=True)
        initialized = True
    if text == "block":
        while True:
            signal.pause()
    print("not json", flush=True)
    if text == "fallback":
        result = "First sentence. Second sentence."
    else:
        result = "Done <voice>Changed the files and need review.</voice>"
    if os.environ.get("FAKE_MALFORMED"):
        malformed = [
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": "not an object"},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": None},
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "text"},
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [None]},
            },
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": None}
            ]}},
        ]
        for event in malformed:
            print(json.dumps(event), flush=True)
    permission_denials = []
    terminal_reason = "end_turn"
    if text == "permission denied":
        permission_denials = [{
            "tool_name": "Write",
            "tool_use_id": "toolu_test",
            "reason": "requires_approval"
        }]
        terminal_reason = "completed"
    if os.environ.get("FAKE_DELTAS"):
        print(json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }), flush=True)
    print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Hello world" if os.environ.get("FAKE_DELTAS") else "Assistant progress."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.txt"}},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "b.txt"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "c.txt"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}
    ]}}), flush=True)
    if os.environ.get("FAKE_DELTAS"):
        print(json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": " more"},
            },
        }), flush=True)
    print(json.dumps({"type": "result", "result": result,
                      "terminal_reason": terminal_reason, "stop_reason": "end_turn",
                      "is_error": False, "permission_denials": permission_denials, "num_turns": 1,
                      "session_id": "sid-1", "usage": {}, "total_cost_usd": 0,
                      "modelUsage": {}, "duration_ms": 1}), flush=True)
    if text == "truncated":
        sys.stdout.write("{truncated")
        sys.stdout.flush()
"""


def test_ensure_persistence_mountpoint_writable(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("chown test requires root")

    mountpoint = tmp_path / "session"
    mountpoint.mkdir()
    monkeypatch.setattr(shim.os, "geteuid", lambda: 0)
    monkeypatch.setenv(shim.CLI_UID_ENV, str(shim.DEFAULT_CLI_UID))
    monkeypatch.setenv(shim.CLI_GID_ENV, str(shim.DEFAULT_CLI_GID))

    os.chown(mountpoint, 0, 0)
    shim._ensure_persistence_mountpoint_writable(str(mountpoint))
    ownership = os.stat(mountpoint)
    assert ownership.st_uid == shim.DEFAULT_CLI_UID
    assert ownership.st_gid == shim.DEFAULT_CLI_GID

    shim._ensure_persistence_mountpoint_writable(str(mountpoint))
    ownership = os.stat(mountpoint)
    assert ownership.st_uid == shim.DEFAULT_CLI_UID
    assert ownership.st_gid == shim.DEFAULT_CLI_GID

    shim._ensure_persistence_mountpoint_writable(str(tmp_path / "missing"))


FAKE_CLI_INIT_AFTER_INPUT = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys

lines_path = os.environ["FAKE_LINES"]

def interrupted(_signum, _frame):
    sys.exit(0)

signal.signal(signal.SIGINT, interrupted)
first_line = sys.stdin.readline()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "delayed-sid",
                  "model": "fake", "apiKeySource": "none", "mcp_servers": []}), flush=True)

def respond(line):
    with open(lines_path, "a") as stream:
        stream.write(line)
    request = json.loads(line)
    text = request["message"]["content"][0]["text"]
    print(json.dumps({"type": "result", "result": "Done <voice>Processed %s.</voice>" % text,
                      "terminal_reason": "end_turn", "session_id": "delayed-sid"}), flush=True)

respond(first_line)
for line in sys.stdin:
    respond(line)
"""


FAKE_CODEX_CLI = r"""#!/usr/bin/env python3
import datetime
import json
import os
import sys
import time

args_path = os.environ.get("FAKE_CODEX_ARGS")
if args_path:
    with open(args_path, "a") as stream:
        json.dump(sys.argv[1:], stream)
        stream.write("\n")
# CODEX_HOME must be a writable dir under the WORKSPACE (the child's cwd), not
# $HOME: the guest's $HOME is read-only rootfs and the real CLI refuses to start
# when the dir does not exist.
assert os.environ.get("CODEX_HOME", "") == os.path.join(os.getcwd(), ".codex")
assert os.path.isdir(os.environ["CODEX_HOME"])
assert "OPENAI_API_KEY" not in os.environ
auth_path = os.path.join(os.environ["CODEX_HOME"], "auth.json")
assert os.path.isfile(auth_path)
auth = json.load(open(auth_path))
assert set(auth) == {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
assert auth["auth_mode"] == "chatgpt"
assert auth["OPENAI_API_KEY"] is None
assert set(auth["tokens"]) == {"id_token", "access_token", "refresh_token", "account_id"}
assert len(auth["tokens"]["access_token"].split(".")) == 3
assert datetime.datetime.fromisoformat(auth["last_refresh"].replace("Z", "+00:00")).year >= 2099
config_path = os.path.join(os.environ["CODEX_HOME"], "config.toml")
assert os.path.isfile(config_path)
config = open(config_path).read()
assert 'model_provider = "ember-openai"' in config
assert 'base_url = "http://chatgpt.com/backend-api/codex/"' in config
assert 'chatgpt_base_url = "http://chatgpt.com/backend-api/"' in config
assert "enable_codex_api_key_env = false" in config
assert 'wire_api = "responses"' in config
assert sys.argv[1] == "app-server"

rpc_path = os.environ.get("FAKE_CODEX_RPC")
scenario = os.environ.get("FAKE_CODEX_SCENARIO", "")

def record(value):
    if rpc_path:
        with open(rpc_path, "a") as stream:
            json.dump(value, stream)
            stream.write("\n")

def emit(value):
    print(json.dumps(value), flush=True)

def response(request, result=None, error=None):
    value = {"jsonrpc": "2.0", "id": request["id"]}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result or {}
    emit(value)

initialize = json.loads(sys.stdin.readline())
record(initialize)
assert initialize["method"] == "initialize"
response(initialize, {"cliVersion": "0.146.0"})
initialized = json.loads(sys.stdin.readline())
record(initialized)
assert initialized["method"] == "initialized"

for line in sys.stdin:
    request = json.loads(line)
    record(request)
    method = request.get("method")
    if "id" not in request:
        response({"id": None}, error={"code": -32000, "message": "server request denied"})
        continue
    if method == "thread/start":
        response(request, {"thread": {"id": "codex-thread"}, "model": "gpt-5.6-luna", "cwd": "/workspace"})
        emit({"jsonrpc": "2.0", "method": "thread/started", "params": {"thread": {"id": "codex-thread"}}})
    elif method == "thread/resume":
        params = request.get("params", {})
        thread_id = params.get("threadId")
        if scenario == "resume-not-found-no-path":
            response(request, error={"code": -32004, "message": "thread not found"})
        else:
            response(request, {"thread": {"id": thread_id}, "model": "gpt-5.6-luna", "cwd": "/workspace"})
            emit({"jsonrpc": "2.0", "method": "thread/resumed", "params": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        params = request.get("params", {})
        emit({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
        if scenario == "death-mid-turn":
            print("fake codex died mid-turn", file=sys.stderr, flush=True)
            sys.exit(17)
        if scenario != "no-tools":
            emit({"jsonrpc": "2.0", "method": "item/started", "params": {"item": {"type": "commandExecution", "command": "echo test"}}})
        if os.environ.get("FAKE_CODEX_SLEEP"):
            time.sleep(float(os.environ["FAKE_CODEX_SLEEP"]))
        emit({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Done <voice>Codex completed the work.</voice>"}}})
        emit({"jsonrpc": "2.0", "method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"last": {"inputTokens": 3, "outputTokens": 4, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 7}}}})
        emit({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "turn-1"}}})
    elif method == "turn/interrupt":
        emit({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "turn-1"}}})
    else:
        response(request, error={"code": -32601, "message": "unknown method"})
"""


FAKE_PI_CLI = r"""#!/usr/bin/env python3
import json
import os
import sys
import time

args_path = os.environ.get("FAKE_PI_ARGS")
if args_path:
    with open(args_path, "a") as stream:
        json.dump(sys.argv[1:], stream)
        stream.write("\n")
assert "--provider" in sys.argv
assert sys.argv[sys.argv.index("--provider") + 1] == "openai-completions"
assert sys.argv[sys.argv.index("--model") + 1] == "qwen3.6-27b"
for flag in ("--no-context-files", "--no-extensions", "--no-skills", "--no-prompt-templates"):
    assert flag in sys.argv
assert sys.argv[sys.argv.index("--extension") + 1] == "/usr/share/ember-pi/extensions/web-research.ts"
assert "--tools" not in sys.argv
assert "disposable Firecracker microVM" in sys.argv[sys.argv.index("--system-prompt") + 1]
rpc_path = os.environ.get("FAKE_PI_RPC")
state = {"sessionId": "pi-session", "model": {"id": "qwen3.6-27b"}}
def record(value):
    if rpc_path:
        with open(rpc_path, "a") as stream:
            json.dump(value, stream)
            stream.write("\n")
def emit(value):
    print(json.dumps(value), flush=True)
def response(command, success=True, data=None):
    value = {"type": "response", "command": command, "success": success}
    if data is not None:
        value["data"] = data
    emit(value)
for line in sys.stdin:
    request = json.loads(line)
    record(request)
    command = request["type"]
    if command == "get_state":
        response(command, data=state)
    elif command == "set_model":
        state["model"] = {"id": request["modelId"]}
        response(command, data=state["model"])
    elif command == "set_thinking_level":
        response(command, data={"level": request["level"]})
    elif command == "switch_session":
        if os.environ.get("FAKE_PI_SWITCH") == "failed":
            response(command, success=False)
        else:
            state["sessionId"] = os.path.basename(request["sessionPath"]).split(".")[0]
            response(command, data={"cancelled": os.environ.get("FAKE_PI_SWITCH") == "cancelled"})
    elif command == "prompt":
        response(command)
        if os.environ.get("FAKE_PI_MODE") == "death":
            print("fake pi died mid-turn", file=sys.stderr, flush=True)
            sys.exit(17)
        if os.environ.get("FAKE_PI_MODE") == "interruptible":
            emit({"type": "agent_start"})
            abort_request = json.loads(sys.stdin.readline())
            record(abort_request)
            response("abort")
            emit({"type": "agent_end", "messages": []})
            continue
        if os.environ.get("FAKE_PI_SLEEP"):
            time.sleep(float(os.environ["FAKE_PI_SLEEP"]))
        if os.environ.get("FAKE_PI_MODE") == "provider-error":
            emit({"type": "agent_end", "messages": [{"role": "assistant", "content": [],
                  "errorMessage": "provider failed: ECONNREFUSED"}]})
        elif os.environ.get("FAKE_PI_MODE") == "textless":
            emit({"type": "agent_end", "messages": []})
        elif os.environ.get("FAKE_PI_MODE") == "telemetry":
            emit({"type": "message_start", "message": {"role": "assistant"}})
            emit({"type": "message_end", "message": {"role": "assistant",
                  "content": [], "stopReason": "toolUse", "usage": {}}})
            emit({"type": "tool_execution_start", "toolCallId": "read-1",
                  "toolName": "read", "args": {"path": "a.txt"}})
            emit({"type": "tool_execution_end", "toolCallId": "read-1"})
            emit({"type": "tool_execution_start", "toolCallId": "bash-1",
                  "toolName": "bash", "args": {"command": "echo pi"}})
            emit({"type": "tool_execution_end", "toolCallId": "bash-1"})
            emit({"type": "message_start", "message": {"role": "assistant"}})
            emit({"type": "message_end", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "Telemetry done"}],
                  "stopReason": "stop", "usage": {"input": 5, "output": 7}}})
            emit({"type": "agent_end", "messages": []})
        elif os.environ.get("FAKE_PI_MODE") == "no-tools":
            emit({"type": "message_start", "message": {"role": "assistant"}})
            emit({"type": "message_end", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "No tools needed"}],
                  "stopReason": "stop", "usage": {"input": 2, "output": 3}}})
            emit({"type": "agent_end", "messages": []})
        elif os.environ.get("FAKE_PI_MODE") == "truncated-tool-call":
            emit({"type": "message_start", "message": {"role": "assistant"}})
            message = {"role": "assistant",
                       "content": [{"type": "text",
                                    "text": os.environ["FAKE_PI_PARTIAL"]}],
                       "stopReason": "length",
                       "usage": {"input": 2, "output": 3}}
            if not os.environ.get("FAKE_PI_OMIT_FINISH_REASON"):
                message["finish_reason"] = "length"
            emit({"type": "message_end", "message": message})
            emit({"type": "agent_end", "messages": []})
        elif os.environ.get("FAKE_PI_MODE") == "tool-call-leak-balanced":
            # A complete but malformed tool-call block with junk closing tags
            # that terminates with stopReason: stop
            emit({"type": "message_start", "message": {"role": "assistant"}})
            message = {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<tool_call>\n"
                            "<function=bash>\n"
                            "<parameter=command>\n"
                            "cd /workspace/src/projects/monolith && grep -rn \"on_startup_jobs\" framework/core.py; ...\n"
                            "</parameter>\n"
                            "</framework/core.py>\n"
                            "</invoke>\n"
                            "\n"
                            "</parameter>\n"
                            "</function>\n"
                            "</tool_call>"
                        ),
                    }
                ],
                "stopReason": "stop",
                "usage": {"input": 2, "output": 3},
            }
            emit({"type": "message_end", "message": message})
            emit({"type": "agent_end", "messages": []})
        elif os.environ.get("FAKE_PI_MODE") == "prose-tool-call":
            emit({"type": "message_start", "message": {"role": "assistant"}})
            emit({"type": "message_end", "message": {"role": "assistant",
                  "content": [{"type": "text", "text":
                               "The response may mention <tool_call> in prose."}],
                  "stopReason": "stop", "usage": {"input": 2, "output": 3}}})
            emit({"type": "agent_end", "messages": []})
        elif os.environ.get("FAKE_PI_MODE") in (
            "repeated-tool-calls",
            "repeated-tool-calls-n-minus-1",
            "repeated-tool-calls-separated",
            "repeated-tool-calls-missing-args",
        ):
            mode = os.environ["FAKE_PI_MODE"]
            # Derived from PI_MAX_IDENTICAL_TOOL_CALLS by the test rather than
            # hardcoded, so raising the threshold cannot silently turn the
            # trip cases into non-trip cases.
            limit = int(os.environ["FAKE_PI_REPEAT_LIMIT"])
            emit({"type": "message_start", "message": {"role": "assistant"}})
            if mode == "repeated-tool-calls":
                tool_args = [{"command": "grep loop shim.py"}] * limit
            elif mode == "repeated-tool-calls-n-minus-1":
                tool_args = [{"command": "grep loop shim.py"}] * (limit - 1)
            elif mode == "repeated-tool-calls-separated":
                tool_args = (
                    [{"command": "grep loop shim.py"}] * (limit - 1)
                    + [{"command": "grep guard shim.py"}]
                    + [{"command": "grep loop shim.py"}] * (limit - 1)
                )
            else:
                tool_args = [None] * limit
            for index, args in enumerate(tool_args):
                event = {"type": "tool_execution_start",
                         "toolCallId": "bash-%d" % index, "toolName": "bash"}
                if args is not None:
                    event["args"] = args
                emit(event)
                emit({"type": "tool_execution_end",
                      "toolCallId": "bash-%d" % index})
            emit({"type": "message_end", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "Loop guard test completed"}],
                  "stopReason": "stop", "usage": {"input": 8, "output": 4}}})
            emit({"type": "agent_end", "messages": []})
        else:
            emit({"type": "tool_start", "toolName": "bash",
                  "args": {"command": "echo pi"}})
            emit({"type": "message_end", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "Done <voice>Pi completed the work.</voice>"}],
                  "stopReason": "stop", "usage": {"input": 5, "output": 7}}})
            emit({"type": "agent_end", "messages": []})
    elif command == "abort":
        response(command)
        emit({"type": "agent_end", "messages": []})
"""


FAKE_EMPTY_CODEX_CLI = r"""#!/usr/bin/env python3
import sys

print("codex empty event stream", file=sys.stderr, flush=True)
"""


FAKE_EMPTY_PI_CLI = r"""#!/usr/bin/env python3
import sys

print("pi empty event stream", file=sys.stderr, flush=True)
"""


def test_fake_cli_fixture_syntax_is_valid():
    """Verify the FAKE_CLI embedded script is valid Python."""
    ast.parse(FAKE_CLI)


def test_fake_codex_cli_fixture_syntax_is_valid():
    ast.parse(FAKE_CODEX_CLI)


def test_fake_pi_cli_fixture_syntax_is_valid():
    ast.parse(FAKE_PI_CLI)


def _codex_manager(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-codex"
    executable.write_text(FAKE_CODEX_CLI)
    os.chmod(executable, 0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_CODEX_ARGS", str(tmp_path / "codex-args.jsonl"))
    monkeypatch.setenv("FAKE_CODEX_RPC", str(tmp_path / "codex-rpc.jsonl"))
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    return shim.CodexProcess(str(workspace), str(executable))


def _pi_manager(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-pi"
    executable.write_text(FAKE_PI_CLI)
    os.chmod(executable, 0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_PI_ARGS", str(tmp_path / "pi-args.jsonl"))
    monkeypatch.setenv("FAKE_PI_RPC", str(tmp_path / "pi-rpc.jsonl"))
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    return shim.PiProcess(str(workspace), str(executable))


def _manager_with_empty_cli(tmp_path, monkeypatch, cli, process_type):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "empty-cli"
    executable.write_text(cli)
    os.chmod(executable, 0o755)
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    return process_type(str(workspace), str(executable))


def test_pi_empty_event_stream_raises_error(tmp_path, monkeypatch):
    manager = _manager_with_empty_cli(
        tmp_path, monkeypatch, FAKE_EMPTY_PI_CLI, shim.PiProcess
    )
    with pytest.raises(RuntimeError, match="pi empty event stream") as exc_info:
        manager.turn("hello", model="qwen")
    assert "exit code 0" in str(exc_info.value)


def test_codex_empty_event_stream_raises_error(tmp_path, monkeypatch):
    manager = _manager_with_empty_cli(
        tmp_path, monkeypatch, FAKE_EMPTY_CODEX_CLI, shim.CodexProcess
    )
    with pytest.raises(RuntimeError, match="codex empty event stream") as exc_info:
        manager.turn("hello", model="luna")
    assert "exit code 0" in str(exc_info.value)


def test_pi_first_turn_returns_text_session_and_usage(tmp_path, monkeypatch):
    manager = _pi_manager(tmp_path, monkeypatch)
    record = manager.turn("hello", model="qwen")
    assert record["result"] == "Done <voice>Pi completed the work.</voice>"
    assert record["session_id"] == "pi-session"
    assert "input_tokens" in record["usage"]
    assert record["activities"] == [{"type": "bash", "command": "echo pi"}]
    manager._close_process()


@pytest.mark.parametrize(
    "partial",
    [
        "<tool_call>\n<",
        "<tool_call>\n<function=bash",
        "<tool_call>\n<function=bash>\n<parameter",
        "<tool_call>\n<function=bash>\n<parameter=command>\nls -",
        (
            "<tool_call>\n<function=bash>\n<parameter=command>\n"
            "ls -la /tmp\n</parameter>\n</"
        ),
    ],
)
def test_pi_truncated_tool_call_fails_turn(tmp_path, monkeypatch, capsys, partial):
    monkeypatch.setenv("FAKE_PI_MODE", "truncated-tool-call")
    monkeypatch.setenv("FAKE_PI_PARTIAL", partial)
    manager = _pi_manager(tmp_path, monkeypatch)

    with pytest.raises(
        RuntimeError,
        match="tool-call syntax",
    ):
        manager.turn("hello", model="qwen")
    manager._close_process()

    assert "pi-tool-call-leak terminal_reason=length" in capsys.readouterr().err


def test_pi_tool_call_leak_balanced_block_fails_turn(tmp_path, monkeypatch, capsys):
    """Test that a complete but malformed tool call block (balanced tags with junk content) fails."""
    monkeypatch.setenv("FAKE_PI_MODE", "tool-call-leak-balanced")
    manager = _pi_manager(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="tool-call syntax"):
        manager.turn("hello", model="qwen")
    manager._close_process()

    assert "pi-tool-call-leak terminal_reason=stop" in capsys.readouterr().err


def test_pi_prose_tool_call_mention_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "prose-tool-call")
    manager = _pi_manager(tmp_path, monkeypatch)

    record = manager.turn("hello", model="qwen")
    manager._close_process()

    assert record["result"] == "The response may mention <tool_call> in prose."


def test_resolve_thinking_level():
    assert shim._resolve_thinking_level(None) == shim.PI_DEFAULT_THINKING_LEVEL
    assert shim._resolve_thinking_level(True) == "high"
    assert shim._resolve_thinking_level(False) == "off"
    assert shim._resolve_thinking_level("medium") == "medium"
    assert shim._resolve_thinking_level("bogus") == shim.PI_DEFAULT_THINKING_LEVEL


@pytest.mark.parametrize(
    ("thinking", "expected"),
    [("high", "high"), (None, shim.PI_DEFAULT_THINKING_LEVEL)],
)
def test_pi_turn_sets_thinking_level_before_prompt(
    tmp_path, monkeypatch, thinking, expected
):
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen", thinking=thinking)
    requests = [
        json.loads(line)
        for line in (tmp_path / "pi-rpc.jsonl").read_text().splitlines()
    ]
    commands = [request["type"] for request in requests]
    thinking_index = commands.index("set_thinking_level")
    assert requests[thinking_index] == {
        "type": "set_thinking_level",
        "level": expected,
    }
    assert thinking_index < commands.index("prompt")
    manager._close_process()


def test_pi_turn_reports_model_and_tool_timing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_PI_MODE", "telemetry")
    clock = [0]

    def timing_now():
        clock[0] += 1
        return float(clock[0])

    monkeypatch.setattr(shim, "_turn_timing_now", timing_now)
    manager = _pi_manager(tmp_path, monkeypatch)
    record = manager.turn("hello", model="qwen")
    manager._close_process()

    assert record["num_turns"] == 2
    assert record["usage"]["model_calls"] == 2
    assert record["usage"]["tool_calls"] == 2
    assert record["usage"]["model_ms"] > 0
    assert record["usage"]["tool_ms"] > 0
    assert record["usage"]["tools_by_name"] == {
        "read": {"calls": 1, "ms": 1000},
        "bash": {"calls": 1, "ms": 1000},
    }
    assert record["activities"] == [
        {"type": "tool_use", "name": "read", "input": {"path": "a.txt"}},
        {"type": "bash", "command": "echo pi"},
    ]
    timing = capsys.readouterr().err
    assert "phase=pi_model calls=2 ms=" in timing
    assert "phase=pi_tools calls=2 ms=" in timing
    assert "bash=1:1000" in timing
    assert "read=1:1000" in timing


def test_pi_turn_without_tools_reports_zero_tool_time(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "no-tools")
    manager = _pi_manager(tmp_path, monkeypatch)
    record = manager.turn("hello", model="qwen")
    manager._close_process()

    assert record["num_turns"] == 1
    assert record["usage"]["tool_calls"] == 0
    assert record["usage"]["tool_ms"] == 0
    assert record["usage"]["tools_by_name"] == {}


def test_pi_repeated_tool_calls_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "repeated-tool-calls")
    monkeypatch.setenv("FAKE_PI_REPEAT_LIMIT", str(shim.PI_MAX_IDENTICAL_TOOL_CALLS))
    manager = _pi_manager(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="repeated the same bash tool call"):
        manager.turn("hello", model="qwen")


def test_pi_repeated_tool_calls_n_minus_1_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "repeated-tool-calls-n-minus-1")
    monkeypatch.setenv("FAKE_PI_REPEAT_LIMIT", str(shim.PI_MAX_IDENTICAL_TOOL_CALLS))
    manager = _pi_manager(tmp_path, monkeypatch)
    record = manager.turn("hello", model="qwen")
    manager._close_process()

    assert "Loop guard test completed" in record["result"]


def test_pi_repeated_tool_calls_separated_resets_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "repeated-tool-calls-separated")
    monkeypatch.setenv("FAKE_PI_REPEAT_LIMIT", str(shim.PI_MAX_IDENTICAL_TOOL_CALLS))
    manager = _pi_manager(tmp_path, monkeypatch)
    record = manager.turn("hello", model="qwen")
    manager._close_process()

    assert "Loop guard test completed" in record["result"]


def test_pi_repeated_tool_calls_missing_args_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "repeated-tool-calls-missing-args")
    monkeypatch.setenv("FAKE_PI_REPEAT_LIMIT", str(shim.PI_MAX_IDENTICAL_TOOL_CALLS))
    manager = _pi_manager(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="repeated the same bash tool call"):
        manager.turn("hello", model="qwen")


def test_pi_pushes_progress_during_turn(tmp_path, monkeypatch):
    pushes = []

    class FakePusher:
        def __init__(self, token):
            assert token == "pi-token"

        def push(self, text, activities):
            pushes.append((text, activities))

        def stop(self):
            pass

    monkeypatch.setattr(shim, "_ProgressPusher", FakePusher)
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen", progress_token="pi-token")
    manager._close_process()

    assert pushes
    assert pushes[0] == ("", [{"type": "bash", "command": "echo pi"}])
    assert all(
        set(payload) == {"partial_text", "activities"}
        for payload in [
            {"partial_text": text, "activities": activities}
            for text, activities in pushes
        ]
    )


def test_pi_no_progress_without_token(tmp_path, monkeypatch):
    class UnexpectedPusher:
        def __init__(self, _token):
            pytest.fail("progress pusher should not be created")

    monkeypatch.setattr(shim, "_ProgressPusher", UnexpectedPusher)
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen")
    manager._close_process()


def test_pi_failing_progress_pusher_does_not_fail_turn(tmp_path, monkeypatch):
    class FailingPusher:
        def __init__(self, _token):
            raise RuntimeError("pusher failed")

    monkeypatch.setattr(shim, "_ProgressPusher", FailingPusher)
    manager = _pi_manager(tmp_path, monkeypatch)
    record = manager.turn("hello", model="qwen", progress_token="pi-token")
    manager._close_process()
    assert record["result"].startswith("Done ")


def test_pi_rpc_reuses_process_and_records_commands(tmp_path, monkeypatch):
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("first", model="qwen")
    first_process = manager.process
    manager.turn("second", model="qwen")
    assert manager.process is first_process
    requests = [
        json.loads(line)
        for line in (tmp_path / "pi-rpc.jsonl").read_text().splitlines()
    ]
    assert [request["type"] for request in requests].count("get_state") == 3
    assert [request["type"] for request in requests].count("prompt") == 2
    assert [request["type"] for request in requests].count("switch_session") == 0
    manager._close_process()


def test_pi_textless_terminal_event_surfaces_error_message(tmp_path, monkeypatch):
    # A provider failure arrives as errorMessage on a textless assistant
    # message (pi docs/custom-provider.md); it must become a turn ERROR, not
    # an empty success (live turns persisted blank records, #4252).
    monkeypatch.setenv("FAKE_PI_MODE", "provider-error")
    manager = _pi_manager(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        manager.turn("hello", model="qwen")
    assert "ECONNREFUSED" in str(excinfo.value)


def test_pi_cancelled_switch_raises_session_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_SWITCH", "cancelled")
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("first", model="qwen")
    manager.session_id = None
    with pytest.raises(shim.SessionConflictError, match="pi-session"):
        manager.turn("resume", session_id="pi-session", model="qwen")
    manager._close_process()


def test_pi_failed_switch_raises_session_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_SWITCH", "failed")
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("first", model="qwen")
    manager.session_id = None
    with pytest.raises(
        shim.SessionConflictError, match="switch_session failed.*pi-session"
    ):
        manager.turn("resume", session_id="pi-session", model="qwen")
    manager._close_process()


def test_pi_interrupt_sends_abort(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "interruptible")
    manager = _pi_manager(tmp_path, monkeypatch)
    result = [None]
    exception = [None]

    def run_turn():
        try:
            result[0] = manager.turn("block", model="qwen")
        except Exception as exc:
            exception[0] = exc

    thread = threading.Thread(target=run_turn)
    thread.start()
    time.sleep(0.2)
    interrupt_result = manager.interrupt()
    thread.join(timeout=2)
    requests = [
        json.loads(line)
        for line in (tmp_path / "pi-rpc.jsonl").read_text().splitlines()
    ]
    assert any(request["type"] == "abort" for request in requests)
    assert interrupt_result == {
        "terminal_reason": "user_interrupt",
        "killed": False,
        "timeout": False,
    }
    assert thread.is_alive() is False
    assert "pi turn produced no output" in str(exception[0])
    manager._close_process()


def test_pi_read_timeout_respawns(tmp_path, monkeypatch):
    monkeypatch.setattr(shim, "TURN_READ_TIMEOUT", 0.1)
    monkeypatch.setenv("FAKE_PI_SLEEP", "1")
    manager = _pi_manager(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="timed out waiting for Pi output"):
        manager.turn("slow", model="qwen")
    assert manager.process is None
    monkeypatch.delenv("FAKE_PI_SLEEP")
    manager.turn("again", model="qwen")
    manager._close_process()


def test_pi_death_mid_turn_includes_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODE", "death")
    manager = _pi_manager(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        manager.turn("die", model="qwen")
    assert "fake pi died mid-turn" in str(excinfo.value)
    assert "exit code 17" in str(excinfo.value)
    manager._close_process()


def test_pi_resume_uses_session_flag(tmp_path, monkeypatch):
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("first", model="qwen")
    manager.session_id = None
    manager.turn("second", session_id="pi-session", model="qwen")
    requests = [
        json.loads(line)
        for line in (tmp_path / "pi-rpc.jsonl").read_text().splitlines()
    ]
    switch = next(
        request for request in requests if request["type"] == "switch_session"
    )
    assert switch["sessionPath"].endswith("/sessions/pi-session.jsonl")
    manager._close_process()


def test_manager_routes_qwen_to_pi(tmp_path, monkeypatch):
    manager = shim.ProcessManager(
        tmp_path / "workspace",
        tmp_path / "fake-claude",
        tmp_path / "fake-codex",
        tmp_path / "fake-pi",
    )
    assert manager._adapter("qwen") is manager.pi
    assert manager._adapter("luna") is manager.codex
    assert manager._adapter(None) is manager.claude


def _adapter_manager(prewarm_clis):
    manager = _new_process_manager()
    manager._prewarm_clis = prewarm_clis
    manager.claude = object()
    manager.codex = object()
    manager.pi = object()
    return manager


def test_manager_routes_unspecified_model_to_only_configured_cli():
    manager = _adapter_manager(("pi",))

    assert manager._adapter(None) is manager.pi


def test_manager_routes_unspecified_model_to_claude_when_configured():
    manager = _adapter_manager(("claude", "codex", "pi"))

    assert manager._adapter(None) is manager.claude


@pytest.mark.parametrize("prewarm_clis", [("pi",), ("claude", "codex", "pi")])
def test_manager_explicit_model_routing_precedes_cli_fallback(prewarm_clis):
    manager = _adapter_manager(prewarm_clis)
    codex_model = next(iter(shim.CODEX_MODELS))

    assert manager._adapter("qwen") is manager.pi
    assert manager._adapter(codex_model) is manager.codex


def test_manager_empty_configured_clis_fall_back_to_claude():
    manager = _adapter_manager(())

    assert manager._adapter(None) is manager.claude


def test_manager_ambiguous_configured_clis_fall_back_to_claude():
    manager = _adapter_manager(("codex", "pi"))

    assert manager._adapter(None) is manager.claude


def test_codex_first_turn_returns_thread_voice_and_usage(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    record = manager.turn("first", model="luna")
    assert record == {
        "result": "Done <voice>Codex completed the work.</voice>",
        "terminal_reason": "completed",
        "session_id": "codex-thread",
        "usage": {
            "input_tokens": 3,
            "output_tokens": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        "voice": "Codex completed the work.",
        "activities": [{"type": "bash", "command": "echo test"}],
    }
    manager._close_process()


def test_codex_pushes_progress_during_turn(tmp_path, monkeypatch):
    pushes = []

    class FakePusher:
        def __init__(self, token):
            assert token == "codex-token"

        def push(self, text, activities):
            pushes.append((text, activities))

        def stop(self):
            pass

    monkeypatch.setattr(shim, "_ProgressPusher", FakePusher)
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("first", model="luna", progress_token="codex-token")
    manager._close_process()

    assert len(pushes) >= 2
    assert pushes[0][1] == [{"type": "bash", "command": "echo test"}]
    assert pushes[-1] == (
        "Done <voice>Codex completed the work.</voice>",
        pushes[-1][1],
    )


def test_codex_no_progress_without_token(tmp_path, monkeypatch):
    class UnexpectedPusher:
        def __init__(self, _token):
            pytest.fail("progress pusher should not be created")

    monkeypatch.setattr(shim, "_ProgressPusher", UnexpectedPusher)
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("first", model="luna")
    manager._close_process()


def test_codex_failing_progress_pusher_does_not_fail_turn(tmp_path, monkeypatch):
    class FailingPusher:
        def __init__(self, _token):
            raise RuntimeError("pusher failed")

    monkeypatch.setattr(shim, "_ProgressPusher", FailingPusher)
    manager = _codex_manager(tmp_path, monkeypatch)
    record = manager.turn("first", model="luna", progress_token="codex-token")
    manager._close_process()
    assert record["result"].startswith("Done ")


def test_codex_auth_json_has_inert_subscription_schema(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    child_env = manager._child_env()
    manager._write_auth_json(child_env["CODEX_HOME"])
    auth = json.loads((tmp_path / "workspace" / ".codex" / "auth.json").read_text())
    assert set(auth) == {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
    assert auth["auth_mode"] == "chatgpt"
    assert auth["OPENAI_API_KEY"] is None
    assert set(auth["tokens"]) == {
        "id_token",
        "access_token",
        "refresh_token",
        "account_id",
    }
    assert all(auth["tokens"].values())
    assert (
        datetime.datetime.fromisoformat(
            auth["last_refresh"].replace("Z", "+00:00")
        ).year
        >= 2099
    )


def test_codex_config_uses_subscription_endpoint_override(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    endpoint = "http://broker.test/backend-api/"
    monkeypatch.setenv(shim.CODEX_SUBSCRIPTION_BASE_URL_ENV, endpoint)
    child_env = manager._child_env()
    manager._write_model_config(child_env["CODEX_HOME"])
    config = tomllib.loads(
        (tmp_path / "workspace" / ".codex" / "config.toml").read_text()
    )
    assert (
        config["model_providers"]["ember-openai"]["base_url"]
        == "http://broker.test/backend-api/codex/"
    )
    assert config["chatgpt_base_url"] == endpoint
    assert config["model_provider"] == "ember-openai"
    assert config["sandbox_mode"] == "danger-full-access"
    assert config["approval_policy"] == "never"
    assert config["tools"]["web_search"] is True
    assert config["model_providers"]["ember-openai"]["wire_api"] == "responses"


def test_codex_config_appends_codex_to_no_slash_endpoint(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    endpoint = "http://broker.test/backend-api"
    monkeypatch.setenv(shim.CODEX_SUBSCRIPTION_BASE_URL_ENV, endpoint)
    child_env = manager._child_env()
    manager._write_model_config(child_env["CODEX_HOME"])
    config = tomllib.loads(
        (tmp_path / "workspace" / ".codex" / "config.toml").read_text()
    )
    assert (
        config["model_providers"]["ember-openai"]["base_url"]
        == "http://broker.test/backend-api/codex/"
    )
    assert config["chatgpt_base_url"] == endpoint
    assert config["sandbox_mode"] == "danger-full-access"
    assert config["approval_policy"] == "never"
    assert config["tools"]["web_search"] is True


def test_codex_child_env_drops_openai_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-codex")
    manager = _codex_manager(tmp_path, monkeypatch)
    assert "OPENAI_API_KEY" not in manager._child_env()


def test_codex_resume_uses_positional_session_and_no_sandbox(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    first_record = manager.turn("first", model="terra")
    first_process = manager.process
    second_record = manager.turn("second", session_id="codex-thread", model="terra")
    for record in (first_record, second_record):
        assert record["usage"] == {
            "input_tokens": 3,
            "output_tokens": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        assert record["activities"] == [{"type": "bash", "command": "echo test"}]

    assert manager.process is first_process
    calls = [
        json.loads(line)
        for line in (tmp_path / "codex-args.jsonl").read_text().splitlines()
    ]
    assert calls == [["app-server"]]
    manager._close_process()


def test_codex_app_server_second_turn_no_respawn(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    assert manager.process is None
    manager.turn("first", model="luna")
    first_process = manager.process
    record = manager.turn("second", model="terra")

    assert manager.process is first_process
    assert manager.process.poll() is None
    assert record["usage"]["input_tokens"] == 3
    manager._close_process()


def test_codex_resume_by_thread_id(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("first", model="luna", system_prompt="CALLER-MARKER")
    session_id = manager.session_id
    manager.session_id = None
    manager.turn(
        "second", session_id=session_id, model="luna", system_prompt="CALLER-MARKER"
    )
    assert manager.session_id == session_id
    requests = [
        json.loads(line)
        for line in (tmp_path / "codex-rpc.jsonl").read_text().splitlines()
    ]
    thread_start = next(
        request for request in requests if request["method"] == "thread/start"
    )
    thread_resume = next(
        request for request in requests if request["method"] == "thread/resume"
    )
    # Thread-scoped requests take SandboxMode (a string); only turn/start takes
    # the SandboxPolicy object. Sending the wrong one is silently dropped by the
    # server, which reads as a posture that is set but is not.
    assert thread_start["params"]["sandbox"] == "danger-full-access"
    assert "sandboxPolicy" not in thread_start["params"]
    assert thread_resume["params"]["sandbox"] == "danger-full-access"
    assert "sandboxPolicy" not in thread_resume["params"]
    for request in (thread_start, thread_resume):
        instructions = request["params"]["developerInstructions"]
        assert shim.SANDBOX_PROMPT in instructions
        assert "CALLER-MARKER" in instructions
    manager._close_process()


def test_codex_resume_failure_raises_with_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "resume-not-found-no-path")
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("first", model="luna")
    session_id = manager.session_id
    manager.session_id = None
    with pytest.raises(RuntimeError) as exc_info:
        manager.turn("second", session_id=session_id, model="luna")
    error = str(exc_info.value)
    assert session_id in error
    assert "thread not found" in error
    manager._close_process()


def test_codex_turn_parameters_per_model(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("msg1", model="luna")
    manager.turn("msg2", model="terra")
    requests = [
        json.loads(line)
        for line in (tmp_path / "codex-rpc.jsonl").read_text().splitlines()
    ]
    turn_starts = [request for request in requests if request["method"] == "turn/start"]
    assert len(turn_starts) == 2
    expected = [
        ("codex-thread", "luna"),
        ("codex-thread", "terra"),
    ]
    for request, (thread_id, model) in zip(turn_starts, expected):
        params = request["params"]
        model_name, effort = shim.CODEX_MODELS[model]
        assert params["threadId"] == thread_id
        assert params["model"] == model_name
        assert params["effort"] == effort
        assert params["approvalPolicy"] == "never"
        assert params["cwd"] == str(manager.workspace)
        assert params["sandboxPolicy"] == {"type": "dangerFullAccess"}
    manager._close_process()


def test_codex_read_timeout_kills_server(tmp_path, monkeypatch):
    original_timeout = shim.TURN_READ_TIMEOUT
    monkeypatch.setattr(shim, "TURN_READ_TIMEOUT", 0.1)
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "10")
    manager = _codex_manager(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="timed out waiting for Codex output"):
        manager.turn("msg", model="luna")
    proc = manager.process
    assert proc is None or proc.poll() is not None
    monkeypatch.setattr(shim, "TURN_READ_TIMEOUT", original_timeout)
    manager.turn("second", model="luna")
    manager._close_process()


def test_codex_server_death_mid_turn_raises_with_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "death-mid-turn")
    manager = _codex_manager(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError) as exc_info:
        manager.turn("msg", model="luna")
    assert "fake codex died mid-turn" in str(exc_info.value)
    assert "exit code 17" in str(exc_info.value)
    manager._close_process()


def test_codex_interrupt_during_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "5")
    manager = _codex_manager(tmp_path, monkeypatch)
    result = [None]
    exception = [None]

    def turn_thread():
        try:
            result[0] = manager.turn("msg", model="luna")
        except Exception as exc:
            exception[0] = exc

    thread = threading.Thread(target=turn_thread)
    thread.start()
    time.sleep(0.5)
    interrupt_result = manager.interrupt()
    thread.join(timeout=7)

    assert interrupt_result["terminal_reason"] == "user_interrupt"
    assert interrupt_result["killed"] is False
    assert result[0] is not None
    manager._close_process()


def test_codex_session_conflict_error_unchanged(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("first", model="luna")

    with pytest.raises(
        shim.SessionConflictError,
        match="session_id 'other-thread' does not match active session 'codex-thread'",
    ):
        manager.turn("conflict", session_id="other-thread", model="luna")
    manager._close_process()


def test_codex_turn_with_no_tool_items_has_empty_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "no-tools")
    manager = _codex_manager(tmp_path, monkeypatch)
    record = manager.turn("no tools", model="luna")

    assert record["activities"] == []
    assert record["usage"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    manager._close_process()


def test_manager_routes_only_known_models_to_codex(tmp_path, monkeypatch):
    codex = _codex_manager(tmp_path, monkeypatch)
    manager = shim.ProcessManager(codex.workspace, codex.executable, codex.executable)
    claude = object()
    codex_adapter = object()
    manager.claude = claude
    manager.codex = codex_adapter
    assert manager._adapter("luna") is codex_adapter
    assert manager._adapter("terra") is codex_adapter
    assert manager._adapter("sol") is codex_adapter
    assert manager._adapter(None) is claude
    assert manager._adapter("claude") is claude
    assert manager._adapter("unknown") is claude


def test_manager_turn_syncs_session_volume_for_codex(tmp_path, monkeypatch):
    # A completed turn is the quiescence point park relies on (#4309): the
    # sync must fire exactly once per turn, through the Manager chokepoint,
    # for the codex lane.
    codex = _codex_manager(tmp_path, monkeypatch)
    manager = shim.ProcessManager(codex.workspace, codex.executable, codex.executable)
    sync_calls = []
    monkeypatch.setattr(shim.os, "sync", lambda: sync_calls.append(1))

    manager.turn("first", model="luna")

    assert len(sync_calls) == 1
    manager._close_process()


def test_manager_turn_syncs_session_volume_for_claude(tmp_path, monkeypatch):
    # Same chokepoint, the claude lane: the sync is not adapter-specific.
    claude = _manager(tmp_path, monkeypatch)
    manager = shim.ProcessManager(claude.workspace, claude.executable)
    monkeypatch.setattr(manager.claude, "_configure_git", lambda: None)
    sync_calls = []
    monkeypatch.setattr(shim.os, "sync", lambda: sync_calls.append(1))

    manager.turn("make changes")

    assert len(sync_calls) == 1


def test_manager_turn_syncs_session_volume_even_when_adapter_raises(
    tmp_path, monkeypatch
):
    # A raised turn (a read timeout, a mid-turn CLI death) may still have left
    # durable-worth CLI state; the finally must still fire the sync (#4309).
    codex = _codex_manager(tmp_path, monkeypatch)
    manager = shim.ProcessManager(codex.workspace, codex.executable, codex.executable)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(manager.codex, "turn", _raise)
    sync_calls = []
    monkeypatch.setattr(shim.os, "sync", lambda: sync_calls.append(1))

    with pytest.raises(RuntimeError, match="boom"):
        manager.turn("first", model="luna")

    assert len(sync_calls) == 1


def _capture_spawn_kwargs(tmp_path, monkeypatch, geteuid, env=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = shim.ClaudeProcess.__new__(shim.ClaudeProcess)
    manager.workspace = str(workspace)
    manager.executable = "claude"
    manager.fatal_error = None
    monkeypatch.setattr(manager, "_configure_git", lambda: None)
    monkeypatch.setattr(shim.os, "geteuid", lambda: geteuid)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    captured = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capturing Popen kwargs")

    monkeypatch.setattr(shim.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError, match="stop after capturing"):
        manager._spawn()
    return captured


def test_spawn_does_not_drop_privileges_when_shim_is_unprivileged(
    tmp_path, monkeypatch
):
    kwargs = _capture_spawn_kwargs(tmp_path, monkeypatch, geteuid=1000)
    assert "user" not in kwargs
    assert "group" not in kwargs


def test_spawn_drops_to_default_cli_identity_when_shim_is_root(tmp_path, monkeypatch):
    kwargs = _capture_spawn_kwargs(tmp_path, monkeypatch, geteuid=0)
    assert kwargs["user"] == 65532
    assert kwargs["group"] == 65532


def test_spawn_honors_cli_identity_environment_overrides(tmp_path, monkeypatch):
    kwargs = _capture_spawn_kwargs(
        tmp_path,
        monkeypatch,
        geteuid=0,
        env={shim.CLI_UID_ENV: "1234", shim.CLI_GID_ENV: "2345"},
    )
    assert kwargs["user"] == 1234
    assert kwargs["group"] == 2345


def test_spawn_keeps_claude_stdin_as_a_pipe(tmp_path, monkeypatch):
    # claude's stdin IS its turn channel (--input-format stream-json); pin
    # this so a future stdin.DEVNULL change elsewhere never lands here too
    # (#4303).
    kwargs = _capture_spawn_kwargs(tmp_path, monkeypatch, geteuid=1000)
    assert kwargs["stdin"] is subprocess.PIPE


def test_spawn_keeps_codex_stdin_as_a_pipe(tmp_path, monkeypatch):
    # The app-server JSON-RPC channel rides stdin, so Codex must receive a
    # pipe rather than the DEVNULL stdin used by one-shot CLIs.
    codex = _codex_manager(tmp_path, monkeypatch)
    codex._spawn()
    assert codex.process.stdin is not subprocess.DEVNULL
    codex._close_process(kill=True)


def test_spawn_keeps_pi_stdin_as_a_pipe(tmp_path, monkeypatch):
    pi = _pi_manager(tmp_path, monkeypatch)
    captured = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capturing Popen kwargs")

    monkeypatch.setattr(shim.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError, match="stop after capturing"):
        pi._spawn("qwen")
    assert captured["stdin"] is subprocess.PIPE


def _manager(tmp_path, monkeypatch, api_key="none"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    executable.write_text(FAKE_CLI)
    os.chmod(executable, 0o755)
    monkeypatch.setenv("FAKE_API_KEY", api_key)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(manager, "_configure_git", lambda: None)
    return manager


def _new_process_manager():
    manager = object.__new__(shim.ProcessManager)
    manager._prewarm_clis = ()
    manager._mount_lock = threading.Lock()
    manager._thread_factory = threading.Thread
    return manager


class _FakeStdin:
    def __init__(self):
        self.lines = []

    def write(self, value):
        self.lines.append(value)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeLiveProcess:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.returncode = None
        self.pid = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode


def _parked_claude(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = object.__new__(shim.ClaudeProcess)
    manager.workspace = str(workspace)
    manager.process = _FakeLiveProcess()
    manager.init_event = None
    manager.fatal_error = None
    manager.session_id = None
    manager.model = None
    manager.system_prompt = None
    manager._process_workspace = manager.workspace
    stat = os.stat(manager.workspace)
    manager._process_workspace_identity = (stat.st_dev, stat.st_ino)
    manager._manager = None
    manager.turn_lock = threading.Lock()
    manager.process_lock = threading.Lock()
    manager.current_result = None
    manager._stdout_queue = object()
    manager.unparseable_lines = []
    manager.stderr_lines = []
    manager.parsed_events = []
    return manager


def test_user_message_line_includes_optional_session_id():
    assert json.loads(shim._user_message_line("hello")) == {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    assert (
        json.loads(shim._user_message_line("hello", session_id="sid"))["session_id"]
        == "sid"
    )
    assert "session_id" not in json.loads(
        shim._user_message_line("hello", session_id="")
    )


def test_process_manager_prewarm_marks_ready_after_park(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace" / "src"
    workspace.mkdir(parents=True)
    manager = _new_process_manager()
    manager._prewarm_clis = ("claude",)
    manager._prewarm_complete = False
    manager.fatal_error = None
    calls = []

    class Adapter:
        session_id = None
        fatal_error = None
        process = _FakeLiveProcess()

        def _spawn(self, **kwargs):
            calls.append(("spawn", kwargs))

        def ready(self):
            return True

    manager.claude = Adapter()
    manager.claude.workspace = str(workspace)
    temp_a = Adapter()
    temp_a.turn_lock = threading.Lock()
    manager.codex = temp_a
    temp_b = Adapter()
    temp_b.turn_lock = threading.Lock()
    manager.pi = temp_b
    manager._close_process = lambda **_kwargs: None
    monkeypatch.setattr(
        shim, "apply_egress_ca_trust", lambda: calls.append(("trust", None))
    )
    manager.prewarm()

    # Mock the external state checks so ready() returns True.
    monkeypatch.setattr(shim, "_workspace_is_tmpfs", lambda: False)
    monkeypatch.setattr(shim, "_volume_has_ext4", lambda: False)

    assert calls == [
        ("trust", None),
        (
            "spawn",
            {
                "session_id": None,
                "first_message": None,
                "model": None,
                "init_timeout": 60,
            },
        ),
    ]
    assert manager.claude.session_id is None
    assert manager._prewarm_complete
    assert manager.ready()


def test_process_manager_prewarm_failure_is_not_ready(tmp_path, monkeypatch):
    manager = _new_process_manager()
    manager._prewarm_clis = ("claude",)
    manager._prewarm_complete = False
    manager.fatal_error = None

    class Adapter:
        session_id = None
        fatal_error = None

        def _spawn(self, **_kwargs):
            raise shim.StartupError("init timeout")

        def ready(self):
            return True

    manager.claude = Adapter()
    manager.claude.workspace = str(tmp_path)
    temp_a = Adapter()
    temp_a.turn_lock = threading.Lock()
    manager.codex = temp_a
    temp_b = Adapter()
    temp_b.turn_lock = threading.Lock()
    manager.pi = temp_b
    manager._close_process = lambda **_kwargs: None
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)
    manager.prewarm()

    assert not manager.ready()
    assert "CLI prewarm failed" in manager.fatal_error


def test_process_manager_prewarm_spawns_every_configured_family_parked(
    tmp_path, monkeypatch
):
    """#4423: codex and pi park like claude, each per its own init profile.

    Claude parks without starting a turn or binding a session, codex binds
    nothing until thread/start, and pi is parked on the default model with no
    caller system prompt so the common first turn reuses it.
    """
    workspace = tmp_path / "workspace" / "src"
    workspace.mkdir(parents=True)
    manager = _new_process_manager()
    manager._prewarm_clis = ("claude", "codex", "pi")
    manager._prewarm_complete = False
    manager.fatal_error = None
    calls = {}

    class Adapter:
        session_id = None
        fatal_error = None
        process = _FakeLiveProcess()

        def _spawn(self, *args, **kwargs):
            calls[self.name] = (args, kwargs)

        def ready(self):
            return True

    for name in ("claude", "codex", "pi"):
        adapter = Adapter()
        adapter.name = name
        adapter.workspace = str(workspace)
        adapter.turn_lock = threading.Lock()
        setattr(manager, name, adapter)
    manager._close_process = lambda **_kwargs: None
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)
    manager.prewarm()

    assert calls["claude"] == (
        (),
        {
            "session_id": None,
            "first_message": None,
            "model": None,
            "init_timeout": 60,
        },
    )
    # Codex's whole init is the app-server spawn plus initialize handshake:
    # model, effort and developer instructions all ride per-turn requests.
    assert calls["codex"] == ((), {})
    # Pi takes model and system prompt at spawn time only.
    assert calls["pi"] == ((shim.DEFAULT_PI_MODEL,), {"system_prompt": None})
    for name in ("claude", "codex", "pi"):
        adapter = getattr(manager, name)
        assert adapter.session_id is None
        assert adapter.process.poll() is None
    assert manager._prewarm_complete


def test_process_manager_prewarm_failure_closes_every_cli(tmp_path, monkeypatch):
    """A half-prewarmed guest must not hold resident CLIs while unready."""
    workspace = tmp_path / "workspace" / "src"
    workspace.mkdir(parents=True)
    manager = _new_process_manager()
    manager._prewarm_clis = ("claude", "codex", "pi")
    manager._prewarm_complete = False
    manager.fatal_error = None

    class Adapter:
        session_id = None
        fatal_error = None

        def __init__(self, name):
            self.name = name
            self.closed = None
            self.process = _FakeLiveProcess()

        def _spawn(self, **_kwargs):
            if self.name == "codex":
                raise shim.StartupError("init timeout")

        def _close_process(self, kill=False):
            self.closed = kill

        def ready(self):
            return True

    for name in ("claude", "codex", "pi"):
        adapter = Adapter(name)
        adapter.workspace = str(workspace)
        adapter.turn_lock = threading.Lock()
        setattr(manager, name, adapter)

    def close_all(kill=False):
        for name in ("claude", "codex", "pi"):
            getattr(manager, name)._close_process(kill=kill)

    manager._close_process = close_all
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)
    manager.prewarm()

    assert "CLI prewarm failed" in manager.fatal_error
    # Codex failed, but claude had already parked: everything closes.
    for name in ("claude", "codex", "pi"):
        assert getattr(manager, name).closed is True


def test_process_manager_prewarm_without_egress_ca_spawns_untrusted_cli(
    tmp_path, monkeypatch
):
    trust_env_path = tmp_path / "trust-env.json"
    monkeypatch.setenv("FAKE_TRUST_ENV", str(trust_env_path))
    for key in EGRESS_CA_TRUST_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(shim, "fetch_egress_ca", lambda: None)

    manager = _new_process_manager()
    manager._prewarm_clis = ("claude",)
    manager._prewarm_complete = False
    manager.fatal_error = None
    manager.claude = _manager(tmp_path, monkeypatch)

    try:
        manager.prewarm()

        assert manager.fatal_error is None
        assert manager._prewarm_complete
        assert json.loads(trust_env_path.read_text()) == {
            key: None for key in EGRESS_CA_TRUST_ENV_KEYS
        }
    finally:
        manager.claude._close_process(kill=True)


def test_process_manager_prewarm_child_inherits_fetched_egress_ca_trust(
    tmp_path, monkeypatch
):
    trust_env_path = tmp_path / "trust-env.json"
    bundle = tmp_path / "ember-ca-bundle.crt"
    system_bundle = tmp_path / "system-ca-bundle.crt"
    system_bundle.write_bytes(b"system certificate\n")
    monkeypatch.setenv("FAKE_TRUST_ENV", str(trust_env_path))
    for key in EGRESS_CA_TRUST_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(shim, "CA_BUNDLE_PATH", str(bundle))
    monkeypatch.setattr(shim, "SYSTEM_CA_BUNDLE", str(system_bundle))
    monkeypatch.setattr(
        shim,
        "fetch_egress_ca",
        lambda: b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
    )

    manager = _new_process_manager()
    manager._prewarm_clis = ("claude",)
    manager._prewarm_complete = False
    manager.fatal_error = None
    manager.claude = _manager(tmp_path, monkeypatch)

    try:
        manager.prewarm()

        assert manager.fatal_error is None
        assert manager._prewarm_complete
        assert json.loads(trust_env_path.read_text()) == {
            key: str(bundle) for key in EGRESS_CA_TRUST_ENV_KEYS
        }
    finally:
        manager.claude._close_process(kill=True)


def test_process_manager_turn_applies_per_turn_trust_after_volume(monkeypatch):
    manager = _new_process_manager()
    calls = []

    class Adapter:
        workspace = None

        def turn(self, *_args, **_kwargs):
            return {"ok": True}

    manager.claude = Adapter()
    temp_a = Adapter()
    temp_a.turn_lock = threading.Lock()
    manager.codex = temp_a
    temp_b = Adapter()
    temp_b.turn_lock = threading.Lock()
    manager.pi = temp_b
    monkeypatch.setattr(
        shim, "ensure_workspace_volume", lambda: calls.append("workspace")
    )
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: calls.append("trust"))
    monkeypatch.setattr(shim, "_sync_session_volume", lambda: None)

    assert manager.turn("hello") == {"ok": True}
    assert calls == ["workspace", "trust"]


@pytest.mark.parametrize("checkout_state", ["missing", "git_failure"])
def test_process_manager_diff_capture_failure_does_not_fail_turn(
    tmp_path, monkeypatch, checkout_state
):
    manager = _new_process_manager()
    manager.workspace = str(tmp_path / "workspace")
    manager._hydration_error = None
    manager._hydration_status = None

    class Adapter:
        workspace = None

        def turn(self, *_args, **_kwargs):
            return {"ok": True}

    manager.claude = Adapter()
    manager.codex = Adapter()
    manager.codex.turn_lock = threading.Lock()
    manager.pi = Adapter()
    manager.pi.turn_lock = threading.Lock()
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)
    monkeypatch.setattr(shim, "_sync_session_volume", lambda: None)
    if checkout_state == "git_failure":
        checkout = tmp_path / "workspace" / "src"
        (checkout / ".git").mkdir(parents=True)
        monkeypatch.setattr(
            shim.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b""),
        )

    assert manager.turn("hello") == {"ok": True}


def test_turn_base_no_git_dir_logs_reason_and_returns_none(tmp_path, capsys):
    checkout = tmp_path / "src"
    checkout.mkdir()

    assert shim._capture_turn_base(str(checkout)) is None
    captured = capsys.readouterr().err
    assert "outcome=no_git_dir" in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_base_rev_parse_failure_logs_reason_and_returns_none(
    monkeypatch, tmp_path, capsys
):
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b""),
    )

    assert shim._capture_turn_base(str(checkout)) is None
    captured = capsys.readouterr().err
    assert "outcome=rev_parse_failed" in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_base_malformed_sha_logs_reason_and_returns_none(
    monkeypatch, tmp_path, capsys
):
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, b"not-a-sha\n"),
    )

    assert shim._capture_turn_base(str(checkout)) is None
    captured = capsys.readouterr().err
    assert "outcome=sha_malformed" in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_base_success_logs_reason_and_returns_sha(monkeypatch, tmp_path, capsys):
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)
    base_sha = "a" * 40
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, (base_sha + "\n").encode("ascii")
        ),
    )

    assert shim._capture_turn_base(str(checkout)) == base_sha
    captured = capsys.readouterr().err
    assert "phase=base outcome=success" in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_diff_no_base_sha_logs_reason_and_returns_none(tmp_path, capsys):
    checkout = tmp_path / "src"

    assert shim._capture_turn_diff(str(checkout), None) is None
    captured = capsys.readouterr().err
    assert "outcome=no_base_sha" in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_diff_failure_logs_reason_and_returns_none(monkeypatch, tmp_path, capsys):
    checkout = tmp_path / "src"
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b""),
    )

    assert shim._capture_turn_diff(str(checkout), "a" * 40) is None
    captured = capsys.readouterr().err
    assert "outcome=diff_failed" in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_diff_success_logs_and_round_trips(monkeypatch, tmp_path, capsys):
    checkout = tmp_path / "src"
    base_sha = "a" * 40
    raw = b"diff --git a/file b/file\n+changed\n"
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, raw),
    )

    result = shim._capture_turn_diff(str(checkout), base_sha)

    assert result == {
        "base_sha": base_sha,
        "zlib_b64": result["zlib_b64"],
        "truncated": False,
    }
    assert zlib.decompress(base64.b64decode(result["zlib_b64"])) == raw
    captured = capsys.readouterr().err
    assert "phase=diff outcome=success" in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_diff_includes_untracked_files_as_new_file_hunks(tmp_path, capsys):
    """A file the agent created must appear in the stored diff (#5051)."""
    checkout = tmp_path / "src"
    checkout.mkdir()
    git = ["git", "-C", str(checkout)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    (checkout / "tracked.txt").write_text("one\n")
    subprocess.run(git + ["add", "tracked.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "base"], check=True)
    base_sha = subprocess.run(
        git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (checkout / "tracked.txt").write_text("one\ntwo\n")
    (checkout / "answer.json").write_text('{"adr": "x"}\n')

    result = shim._capture_turn_diff(str(checkout), base_sha)

    assert result["truncated"] is False
    raw = zlib.decompress(base64.b64decode(result["zlib_b64"])).decode()
    assert "+two" in raw
    assert "answer.json" in raw
    assert "new file mode" in raw
    assert '+{"adr": "x"}' in raw
    # Read-only: the index must not have gained the new file.
    status = subprocess.run(
        git + ["status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout
    assert "?? answer.json" in status


def test_turn_diff_logging_failure_does_not_change_capture(monkeypatch, tmp_path):
    checkout = tmp_path / "src"
    base_sha = "a" * 40
    raw = b"diff bytes"
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, raw),
    )

    def raise_logging_error(*_args, **_kwargs):
        raise OSError("stderr unavailable")

    monkeypatch.setattr(shim.sys.stderr, "write", raise_logging_error)

    result = shim._capture_turn_diff(str(checkout), base_sha)

    assert result["base_sha"] == base_sha
    assert result["truncated"] is False
    assert zlib.decompress(base64.b64decode(result["zlib_b64"])) == raw


@pytest.mark.parametrize(
    ("capture", "expected_reason"),
    [
        (lambda checkout: shim._capture_turn_base(checkout), "base_exception"),
        (
            lambda checkout: shim._capture_turn_diff(checkout, "a" * 40),
            "diff_exception",
        ),
    ],
)
def test_turn_diff_subprocess_exception_logs_and_does_not_propagate(
    capture, expected_reason, monkeypatch, tmp_path, capsys
):
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)

    def raise_subprocess(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(shim.subprocess, "run", raise_subprocess)

    assert capture(str(checkout)) is None
    captured = capsys.readouterr().err
    assert "outcome=%s" % expected_reason in captured
    assert "checkout_dir=%s" % checkout.resolve() in captured


def test_turn_diff_uncompressed_cap_sets_truncated(monkeypatch, tmp_path, capsys):
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)
    raw = b"x" * (shim.MAX_TURN_DIFF_BYTES + 1)
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, raw),
    )

    assert shim._capture_turn_diff(str(checkout), "a" * 40) == {
        "base_sha": "a" * 40,
        "zlib_b64": None,
        "truncated": True,
    }
    assert "outcome=truncated_raw" in capsys.readouterr().err


def test_turn_diff_compressed_cap_sets_truncated(monkeypatch, tmp_path, capsys):
    checkout = tmp_path / "src"
    raw = b"diff bytes"
    monkeypatch.setattr(shim, "MAX_TURN_DIFF_COMPRESSED_BYTES", 1)
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, raw),
    )

    assert shim._capture_turn_diff(str(checkout), "a" * 40) == {
        "base_sha": "a" * 40,
        "zlib_b64": None,
        "truncated": True,
    }
    assert "outcome=truncated_compressed" in capsys.readouterr().err


def test_turn_diff_over_cap_keeps_small_added_files(monkeypatch, tmp_path, capsys):
    checkout = tmp_path / "src"
    big_marker = b"BIG_MODIFIED_CONTENT"
    big = (
        b"diff --git a/big.txt b/big.txt\n"
        b"--- a/big.txt\n"
        b"+++ b/big.txt\n"
        b"@@ -1 +1 @@\n"
        b"+" + big_marker + b"x" * shim.MAX_TURN_DIFF_BYTES + b"\n"
    )
    plan = (
        b"diff --git a/plan.json b/plan.json\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/plan.json\n"
        b"@@ -0,0 +1 @@\n"
        b'+{"steps": []}\n'
    )
    monkeypatch.setattr(shim, "_untracked_file_diffs", lambda _checkout: b"")
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, big + plan),
    )

    result = shim._capture_turn_diff(str(checkout), "a" * 40)

    assert result["zlib_b64"] is not None
    reduced = zlib.decompress(base64.b64decode(result["zlib_b64"]))
    assert result["truncated"] is True
    assert b"plan.json" in reduced
    assert big_marker not in reduced
    assert "outcome=truncated_raw_reduced" in capsys.readouterr().err


def test_turn_diff_reduced_excludes_oversized_added_files(
    monkeypatch, tmp_path, capsys
):
    checkout = tmp_path / "src"
    oversized = (
        b"diff --git a/work.bin b/work.bin\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/work.bin\n"
        b"@@ -0,0 +1 @@\n"
        b"+" + b"x" * shim.MAX_TURN_DIFF_BYTES + b"\n"
    )
    monkeypatch.setattr(shim, "_untracked_file_diffs", lambda _checkout: b"")
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, oversized),
    )

    assert shim._capture_turn_diff(str(checkout), "a" * 40) == {
        "base_sha": "a" * 40,
        "zlib_b64": None,
        "truncated": True,
    }
    assert "outcome=truncated_raw" in capsys.readouterr().err


def test_reduced_diff_keeps_section_bytes_and_ignores_content_headers():
    """Two added files survive byte for byte, and a content line that merely
    contains "diff --git " never starts a section boundary."""
    first = (
        b"diff --git a/a.json b/a.json\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/a.json\n"
        b"@@ -0,0 +1,2 @@\n"
        b"+line one\n"
        b"+diff --git a/decoy b/decoy\n"
    )
    second = (
        b"diff --git a/b.json b/b.json\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/b.json\n"
        b"@@ -0,0 +1 @@\n"
        b"+{}\n"
    )
    modified = (
        b"diff --git a/work.txt b/work.txt\n"
        b"--- a/work.txt\n"
        b"+++ b/work.txt\n"
        b"@@ -1 +1 @@\n"
        b"+changed\n"
    )

    assert shim._reduced_added_file_diff(first + modified + second) == first + second


def test_turn_diff_compressed_cap_keeps_small_added_files(
    monkeypatch, tmp_path, capsys
):
    checkout = tmp_path / "src"
    modified = (
        b"diff --git a/work.bin b/work.bin\n"
        b"--- a/work.bin\n"
        b"+++ b/work.bin\n"
        b"@@ -1 +1 @@\n+" + bytes(range(256)) * 64 + b"\n"
    )
    plan = (
        b"diff --git a/plan.json b/plan.json\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/plan.json\n"
        b"@@ -0,0 +1 @@\n"
        b'+{"steps": ["ship"]}\n'
    )
    reduced_size = len(zlib.compress(plan))
    compressed_cap = reduced_size + 1
    assert len(zlib.compress(modified + plan)) > compressed_cap
    monkeypatch.setattr(shim, "MAX_TURN_DIFF_COMPRESSED_BYTES", compressed_cap)
    monkeypatch.setattr(shim, "_untracked_file_diffs", lambda _checkout: b"")
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, modified + plan),
    )

    result = shim._capture_turn_diff(str(checkout), "a" * 40)

    assert result["truncated"] is True
    assert result["zlib_b64"] is not None
    assert zlib.decompress(base64.b64decode(result["zlib_b64"])) == plan
    assert "outcome=truncated_compressed_reduced" in capsys.readouterr().err


def test_process_manager_captures_diff_against_head_at_turn_start(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    checkout = workspace / "src"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=checkout,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=checkout, check=True
    )
    tracked = checkout / "tracked.txt"
    tracked.write_text("before\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=checkout, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()

    manager = _new_process_manager()
    manager.workspace = str(workspace)
    manager._hydration_error = None
    manager._hydration_status = None

    class Adapter:
        workspace = None

        def turn(self, *_args, **_kwargs):
            tracked.write_text("after\n")
            return {"ok": True}

    manager.claude = Adapter()
    manager.codex = Adapter()
    manager.codex.turn_lock = threading.Lock()
    manager.pi = Adapter()
    manager.pi.turn_lock = threading.Lock()
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)
    monkeypatch.setattr(shim, "_sync_session_volume", lambda: None)

    record = manager.turn("change it")

    assert record["diff"]["base_sha"] == base_sha
    assert record["diff"]["truncated"] is False
    compressed = base64.b64decode(record["diff"]["zlib_b64"])
    assert b"-before\n+after\n" in shim.zlib.decompress(compressed)


def test_process_manager_without_prewarm_preserves_ready_semantics():
    manager = _new_process_manager()
    manager._prewarm_clis = ()
    manager._prewarm_complete = True
    manager.fatal_error = None

    class Adapter:
        def ready(self):
            return True

    manager.claude = Adapter()
    temp_a = Adapter()
    temp_a.turn_lock = threading.Lock()
    manager.codex = temp_a
    temp_b = Adapter()
    temp_b.turn_lock = threading.Lock()
    manager.pi = temp_b
    assert manager.ready()


def test_process_manager_normalizes_non_repo_workspace_to_src(tmp_path, monkeypatch):
    class Adapter:
        def __init__(self, workspace, _executable):
            self.workspace = workspace

        def ready(self):
            return True

    monkeypatch.delenv(shim.PREWARM_CLIS_ENV, raising=False)
    monkeypatch.setattr(
        shim, "_ensure_persistence_mountpoint_writable", lambda _path: None
    )
    monkeypatch.setattr(shim, "_write_git_proxy_helper", lambda: None)
    monkeypatch.setattr(shim, "ClaudeProcess", Adapter)
    monkeypatch.setattr(shim, "CodexProcess", Adapter)
    monkeypatch.setattr(shim, "PiProcess", Adapter)

    manager = shim.ProcessManager(tmp_path / "workspace", "claude", "codex", "pi")

    expected = str(tmp_path / "workspace" / "src")
    assert manager.claude.workspace == expected
    assert os.path.isdir(expected)


def test_missing_transcript_is_a_turn_error(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    process = manager.process
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "No conversation found with session ID: sid",
            }
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)

    with pytest.raises(RuntimeError, match="No conversation found"):
        manager.turn("hello", session_id="sid")
    assert manager.process is None
    assert process.returncode == 0


def test_read_prewarm_clis_rejects_unknown_and_deduplicates(monkeypatch):
    monkeypatch.setenv(shim.PREWARM_CLIS_ENV, " claude, claude,  ")
    assert shim.ProcessManager._read_prewarm_clis() == ("claude",)
    monkeypatch.setenv(shim.PREWARM_CLIS_ENV, "pi, codex , claude\ncodex")
    assert shim.ProcessManager._read_prewarm_clis() == ("pi", "codex", "claude")
    monkeypatch.setenv(shim.PREWARM_CLIS_ENV, "claude, gemini")
    with pytest.raises(shim.StartupError, match="gemini"):
        shim.ProcessManager._read_prewarm_clis()


def test_read_prewarm_clis_falls_back_to_baked_file(tmp_path, monkeypatch):
    """Without the env override the per-image baked list drives prewarm."""
    baked = tmp_path / "prewarm-clis"
    baked.write_text("claude,codex,pi\n")
    monkeypatch.delenv(shim.PREWARM_CLIS_ENV, raising=False)
    monkeypatch.setattr(shim, "PREWARM_CLIS_FILE", str(baked))
    assert shim.ProcessManager._read_prewarm_clis() == ("claude", "codex", "pi")


def test_read_prewarm_clis_baked_file_validates_names(tmp_path, monkeypatch):
    baked = tmp_path / "prewarm-clis"
    baked.write_text("pi, gemini\n")
    monkeypatch.delenv(shim.PREWARM_CLIS_ENV, raising=False)
    monkeypatch.setattr(shim, "PREWARM_CLIS_FILE", str(baked))
    with pytest.raises(shim.StartupError, match="gemini"):
        shim.ProcessManager._read_prewarm_clis()


def test_read_prewarm_clis_env_overrides_baked_file(tmp_path, monkeypatch):
    baked = tmp_path / "prewarm-clis"
    baked.write_text("claude,codex,pi\n")
    monkeypatch.setenv(shim.PREWARM_CLIS_ENV, "pi")
    monkeypatch.setattr(shim, "PREWARM_CLIS_FILE", str(baked))
    assert shim.ProcessManager._read_prewarm_clis() == ("pi",)


def test_read_prewarm_clis_without_env_or_file_disables_prewarm(tmp_path, monkeypatch):
    """An older image without the baked file must boot exactly as before."""
    monkeypatch.delenv(shim.PREWARM_CLIS_ENV, raising=False)
    monkeypatch.setattr(shim, "PREWARM_CLIS_FILE", str(tmp_path / "absent"))
    assert shim.ProcessManager._read_prewarm_clis() == ()


def test_parked_create_latches_session_and_rejects_conflict(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {"type": "result", "result": "ok", "session_id": "created"}
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)
    manager.turn("hello")
    assert manager.session_id == "created"
    with pytest.raises(shim.SessionConflictError):
        manager.turn("wrong", session_id="other")


def test_adoption_latch_rolls_back_before_result(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    monkeypatch.setattr(manager, "_read_output", lambda *_args: b"not-json")
    monkeypatch.setattr(
        manager,
        "_parse_line",
        lambda _raw: (_ for _ in ()).throw(RuntimeError("before result")),
    )
    monkeypatch.setattr(manager, "ready", lambda: True)
    with pytest.raises(RuntimeError, match="before result"):
        manager.turn("hello", session_id="sid")
    assert manager.session_id is None


def test_takeover_remediation_closes_stranded_process_without_respawn(
    tmp_path, monkeypatch
):
    manager = _new_process_manager()
    manager.workspace = str(tmp_path)
    manager._prewarm_clis = ("claude",)
    manager._prewarm_complete = True
    manager.fatal_error = None
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None

    old_process = _FakeLiveProcess()
    new_process = _FakeLiveProcess()

    class Adapter:
        workspace = str(tmp_path / "src")

        def __init__(self):
            self.process = old_process
            self.session_id = None
            self.turn_lock = threading.Lock()
            self._process_workspace_identity = (1, 1)

        def ready(self):
            return True

        def _close_process(self, **_kwargs):
            self.process = None

        def _spawn(self, **_kwargs):
            self.process = new_process

    manager.claude = Adapter()
    manager.codex = manager.claude
    manager.pi = manager.claude
    # The workspace identity already differs from the parked process's (1, 1):
    # the takeover has happened, so a single remediation pass must respawn.
    manager._workspace_identity = lambda _path: (2, 2)
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    monkeypatch.setattr(shim, "_ensure_cli_dir", lambda _path: None)
    monkeypatch.setattr(shim, "_workspace_is_tmpfs", lambda: True)
    monkeypatch.setattr(shim, "_volume_has_ext4", lambda: True)

    assert manager.ready()
    manager._remediation_thread.join(timeout=1)
    # Mount-only remediation (#4393): the stranded process is closed and the
    # respawn is left to the turn path's lazy spawn.
    assert manager.claude.process is None
    assert manager.ready()


def test_takeover_remediation_closes_stranded_prewarms_of_every_family(
    tmp_path, monkeypatch
):
    """#4423 parity: a volume takeover strands parked codex and pi too.

    All three families were spawned against the base's tmpfs workspace; when
    the session volume bind-mounts over /workspace, each parked process holds
    config paths on the hidden tmpfs, so remediation must close all of them
    and let each family's turn path lazy-spawn against the volume.
    """
    manager = _new_process_manager()
    manager.workspace = str(tmp_path)
    manager._prewarm_clis = ("claude", "codex", "pi")
    manager._prewarm_complete = True
    manager.fatal_error = None
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None

    class Adapter:
        workspace = str(tmp_path / "src")

        def __init__(self):
            self.process = _FakeLiveProcess()
            self.session_id = None
            self.turn_lock = threading.Lock()
            self._process_workspace_identity = (1, 1)

        def ready(self):
            return True

        def _close_process(self, **_kwargs):
            self.process = None

    manager.claude = Adapter()
    manager.codex = Adapter()
    manager.pi = Adapter()
    # The takeover has happened: current identity (2, 2) differs from every
    # parked process's spawn-time identity (1, 1).
    manager._workspace_identity = lambda _path: (2, 2)
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    monkeypatch.setattr(shim, "_ensure_cli_dir", lambda _path: None)
    monkeypatch.setattr(shim, "_workspace_is_tmpfs", lambda: True)
    monkeypatch.setattr(shim, "_volume_has_ext4", lambda: True)

    assert manager.ready()
    manager._remediation_thread.join(timeout=1)
    for name in ("claude", "codex", "pi"):
        assert getattr(manager, name).process is None


def test_remediation_recreates_cli_workspace_hidden_by_volume_mount(
    tmp_path, monkeypatch
):
    """A restored guest must stay ready once the volume mount hides src.

    ensure_workspace_volume bind-mounts the volume's empty workspace dir over
    /workspace, so the src dir the base created on tmpfs vanishes. Readiness
    is isdir(src) for every adapter, so remediation has to put it back or the
    prime polls 503 until its deadline (#5051).
    """
    manager = _new_process_manager()
    manager.workspace = str(tmp_path)
    manager._prewarm_clis = ()
    manager._prewarm_complete = True
    manager.fatal_error = None
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None
    src = tmp_path / "src"
    src.mkdir()

    class Adapter:
        workspace = str(src)

        def __init__(self):
            self.process = None
            self.session_id = None
            self.turn_lock = threading.Lock()
            self._process_workspace_identity = None

        def ready(self):
            return os.path.isdir(self.workspace)

        def _close_process(self, **_kwargs):
            self.process = None

    manager.claude = Adapter()
    manager.codex = manager.claude
    manager.pi = manager.claude
    manager._workspace_identity = lambda _path: (1, 1)

    def fake_mount():
        # The bind mount replaces /workspace with an empty volume dir.
        shutil.rmtree(src)

    monkeypatch.setattr(shim, "ensure_workspace_volume", fake_mount)
    monkeypatch.setattr(shim, "_workspace_is_tmpfs", lambda: True)
    monkeypatch.setattr(shim, "_volume_has_ext4", lambda: True)

    assert manager.ready()
    manager._remediation_thread.join(timeout=1)
    assert src.is_dir()
    assert manager.ready()


def test_transcript_slug_replaces_slashes():
    assert shim._transcript_slug("/workspace/src") == "-workspace-src"


def test_volume_probe_requires_ext4_magic(tmp_path, monkeypatch):
    device = tmp_path / "volume"
    device.write_bytes(b"\0" * 0x438 + b"\x53\xef")
    monkeypatch.setenv(shim.VOLUME_DEVICE_ENV, str(device))
    assert shim._volume_has_ext4()
    device.write_bytes(b"\0" * 0x43A)
    assert not shim._volume_has_ext4()


def test_volume_has_ext4_missing_device_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv(shim.VOLUME_DEVICE_ENV, str(tmp_path / "missing"))
    assert not shim._volume_has_ext4()


def test_workspace_is_tmpfs_reads_last_mount(monkeypatch):
    mounts = io.StringIO(
        "tmpfs /workspace tmpfs rw 0 0\n/dev/vdb /workspace ext4 rw 0 0\n"
    )
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: mounts)
    assert not shim._workspace_is_tmpfs()


@pytest.mark.parametrize("dead_cli", ["claude", "codex", "pi"])
def test_ready_build_requires_parked_alive_but_session_is_best_effort(
    monkeypatch, dead_cli
):
    """During a base build every configured prewarm must still be alive."""
    manager = _new_process_manager()
    manager._prewarm_clis = ("claude", "codex", "pi")
    manager._prewarm_complete = True
    manager.fatal_error = None
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None
    # Build-guest state: both probes return False (no filesystem on volume)
    monkeypatch.setattr(shim, "_volume_has_ext4", lambda: False)
    monkeypatch.setattr(shim, "_workspace_is_tmpfs", lambda: False)

    class Adapter:
        def __init__(self, process=None):
            self.process = process
            self.turn_lock = threading.Lock()

        def ready(self):
            return True

    dead = _FakeLiveProcess()
    dead.returncode = 1
    processes = {
        "claude": _FakeLiveProcess(),
        "codex": _FakeLiveProcess(),
        "pi": _FakeLiveProcess(),
    }
    processes[dead_cli] = dead
    manager.claude = Adapter(processes["claude"])
    manager.codex = Adapter(processes["codex"])
    manager.pi = Adapter(processes["pi"])
    # Dead parked CLI in build-guest state -> ready() must return False
    assert not manager.ready()


def test_ready_build_accepts_every_prewarm_alive(monkeypatch):
    """All families parked and alive: the base is accepted as warm."""
    manager = _new_process_manager()
    manager._prewarm_clis = ("claude", "codex", "pi")
    manager._prewarm_complete = True
    manager.fatal_error = None
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None
    monkeypatch.setattr(shim, "_volume_has_ext4", lambda: False)
    monkeypatch.setattr(shim, "_workspace_is_tmpfs", lambda: False)

    class Adapter:
        def __init__(self):
            self.process = _FakeLiveProcess()
            self.turn_lock = threading.Lock()

        def ready(self):
            return True

    manager.claude = Adapter()
    manager.codex = Adapter()
    manager.pi = Adapter()
    assert manager.ready()


def test_remediation_bound_session_closes_without_respawn(tmp_path, monkeypatch):
    manager = _new_process_manager()
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None
    old_process = _FakeLiveProcess()
    old_process.returncode = 1
    calls = []

    class Adapter:
        workspace = str(tmp_path / "src")
        process = old_process
        session_id = "bound-session"
        turn_lock = threading.Lock()
        _process_workspace_identity = (1, 1)

        def _close_process(self, **_kwargs):
            calls.append("close")
            self.process = None

        def _spawn(self, **_kwargs):
            calls.append("spawn")

    manager.claude = Adapter()
    # The remediation pass covers every family now; codex and pi are dormant
    # stubs here (no process, never spawned), so closing them records nothing.
    manager.codex = Adapter()
    manager.pi = Adapter()
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    manager._workspace_identity = lambda _path: (2, 2)
    manager._remediate_workspace()
    assert calls.count("close") == 3
    assert "spawn" not in calls
    assert manager.claude.session_id == "bound-session"


def test_parked_claude_adopts_resume_without_respawn(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {
                "type": "result",
                "result": "ok",
                "terminal_reason": "end_turn",
                "session_id": "sid",
            }
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)
    monkeypatch.setattr(manager, "_spawn", lambda **_kwargs: pytest.fail("respawn"))

    manager.turn("hello", session_id="sid")
    assert json.loads(manager.process.stdin.lines[0])["session_id"] == "sid"


def test_legacy_adoption_respawns_for_workspace_change(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    old_process = manager.process
    new_process = _FakeLiveProcess()
    spawn_calls = []

    def fake_spawn(session_id, first_message, model, **_kwargs):
        spawn_calls.append((session_id, first_message, model, manager.workspace))
        manager.process = new_process

    monkeypatch.setattr(
        shim,
        "_transcript_exists",
        lambda cwd, session_id: cwd == str(tmp_path) and session_id == "sid",
    )
    monkeypatch.setattr(manager, "_spawn", fake_spawn)
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {"type": "result", "result": "ok", "session_id": "sid"}
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)

    manager.turn("hello", session_id="sid")
    assert spawn_calls == [("sid", "hello", None, str(tmp_path / "workspace"))]
    assert manager.process is new_process
    assert old_process.stdin.lines == []


def test_legacy_spawn_fallback_uses_legacy_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = shim.ClaudeProcess(str(workspace), "fake-cli")
    monkeypatch.setattr(manager, "_configure_git", lambda: None)
    monkeypatch.setattr(
        shim,
        "_transcript_exists",
        lambda cwd, session_id: cwd == str(tmp_path) and session_id == "sid",
    )

    captured = {}

    class Process(_FakeLiveProcess):
        stdout = []
        stderr = io.BytesIO()

    process = Process()
    process.stdout = iter(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "sid",
                    "apiKeySource": "none",
                }
            ).encode()
            + b"\n"
        ]
    )

    def fake_popen(_command, **kwargs):
        captured.update(kwargs)
        return process

    monkeypatch.setattr(shim.subprocess, "Popen", fake_popen)
    output = iter(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "sid",
                    "apiKeySource": "none",
                }
            ).encode()
            + b"\n"
        ]
    )
    monkeypatch.setattr(
        manager, "_read_output", lambda _process, _timeout: next(output)
    )
    manager._spawn("sid")
    assert captured["cwd"] == str(tmp_path)


def test_remediation_attempts_cap_at_three(tmp_path, monkeypatch):
    manager = _new_process_manager()
    manager._prewarm_clis = ()
    manager._prewarm_complete = True
    manager.fatal_error = None
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None

    class Adapter:
        workspace = str(tmp_path / "workspace")
        process = None
        session_id = "bound"
        turn_lock = threading.Lock()
        _process_workspace_identity = (1, 1)

        def ready(self):
            return True

    manager.claude = Adapter()
    manager.codex = manager.claude
    manager.pi = manager.claude
    manager._workspace_identity = lambda _path: (1, 1)
    monkeypatch.setattr(shim, "_workspace_is_tmpfs", lambda: True)
    monkeypatch.setattr(shim, "_volume_has_ext4", lambda: True)
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    starts = []

    pending = []

    class DeferredThread:
        def __init__(self, target, name, daemon):
            self.target = target

        def is_alive(self):
            return False

        def start(self):
            starts.append(True)
            pending.append(self.target)

    manager._thread_factory = DeferredThread
    for _ in range(10):
        assert manager.ready()
        # Run the remediation body after start() returns, matching a real
        # thread; running it inline inside start() would deadlock on the
        # _remediation_lock the kick still holds.
        while pending:
            pending.pop(0)()
    assert len(starts) == 3


def test_concurrent_ensure_workspace_volume_serializes(tmp_path, monkeypatch):
    manager = _new_process_manager()
    manager._remediation_lock = threading.Lock()
    manager._remediation_attempts = 0
    manager._remediation_thread = None
    active = False
    concurrent = []
    state_lock = threading.Lock()

    def ensure():
        nonlocal active
        with state_lock:
            if active:
                concurrent.append(True)
            active = True
        time.sleep(0.05)
        with state_lock:
            active = False

    class Adapter:
        workspace = str(tmp_path / "workspace")
        process = None
        session_id = "bound"
        turn_lock = threading.Lock()
        _process_workspace_identity = (1, 1)

        def turn(self, *_args, **_kwargs):
            return {"ok": True}

    manager.claude = Adapter()
    manager.codex = manager.claude
    manager.pi = manager.claude
    manager._workspace_identity = lambda _path: (1, 1)
    monkeypatch.setattr(shim, "ensure_workspace_volume", ensure)
    monkeypatch.setattr(shim, "_ensure_cli_dir", lambda _path: None)
    threads = [
        threading.Thread(target=lambda: manager.turn("hello")),
        threading.Thread(target=manager._remediate_workspace),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
    assert concurrent == []


def test_spawn_raise_during_adoption_leaves_session_unbound(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    monkeypatch.setattr(
        manager,
        "_spawn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            shim.StartupError("spawn failed")
        ),
    )
    manager._process_workspace_identity = (0, 0)

    with pytest.raises(shim.StartupError, match="spawn failed"):
        manager.turn("hello", session_id="sid")
    assert not manager.session_id


def test_parked_claude_create_uses_plain_message(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {
                "type": "result",
                "result": "ok",
                "terminal_reason": "end_turn",
                "session_id": "sid",
            }
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)

    manager.turn("hello")
    assert "session_id" not in json.loads(manager.process.stdin.lines[0])


def test_parked_claude_model_mismatch_respawns_with_resume(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    calls = []

    def respawn(session_id=None, **kwargs):
        calls.append({"session_id": session_id, **kwargs})
        manager.process = _FakeLiveProcess()
        manager._process_workspace = manager.workspace

    monkeypatch.setattr(manager, "_close_process", lambda **_kwargs: None)
    monkeypatch.setattr(manager, "_spawn", respawn)
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {
                "type": "result",
                "result": "ok",
                "terminal_reason": "end_turn",
                "session_id": "sid",
            }
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)

    manager.turn("hello", session_id="sid", model="opus")
    assert calls == [
        {
            "session_id": "sid",
            "first_message": "hello",
            "model": "opus",
            "system_prompt": None,
        }
    ]


def test_claude_turn_timing_reports_spawn_adopt_reuse_and_remediation(
    tmp_path, monkeypatch, capsys
):
    manager = _parked_claude(tmp_path)

    def spawn(session_id=None, first_message=None, model=None, **_kwargs):
        if manager.process is None or manager.process.poll() is not None:
            manager.process = _FakeLiveProcess()
        manager._turn_timing_model_start = shim._turn_timing_now()
        manager.model = model
        manager._process_workspace = manager.workspace

    monkeypatch.setattr(manager, "_spawn", spawn)
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {"type": "result", "result": "ok", "session_id": "sid"}
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)

    manager.turn("first", session_id="sid")
    assert manager.process is not None
    first = capsys.readouterr().err
    assert "phase=cli_ready path=adopt ms=" in first
    assert "phase=model ms=" in first
    assert (
        first.split("phase=cli_ready path=adopt ms=", 1)[1].split("\n", 1)[0].isdigit()
    )

    manager.turn("second", session_id="sid")
    reuse = capsys.readouterr().err
    assert "phase=cli_ready path=reuse ms=" in reuse
    assert "path=lazy_spawn" not in reuse
    assert "phase=model ms=" in reuse

    manager.process.returncode = 0
    manager.turn("third", session_id="sid", model="opus")
    lazy_spawn = capsys.readouterr().err
    assert "phase=cli_ready path=lazy_spawn ms=" in lazy_spawn
    assert "phase=model ms=" in lazy_spawn

    manager.turn("fourth", session_id="sid", model="sonnet")
    remediation = capsys.readouterr().err
    assert "phase=cli_ready path=remediation_respawn ms=" in remediation
    assert "phase=model ms=" in remediation


def test_turn_extracts_voice_activity_and_tolerates_malformed_json(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    record = manager.turn("make changes")
    assert record["terminal_reason"] == "end_turn"
    assert record["voice"] == "Changed the files and need review."
    assert record["activities"] == [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.txt"}},
        {"type": "edit", "file_path": "b.txt"},
        {"type": "write", "file_path": "c.txt"},
        {"type": "bash", "command": "git status"},
    ]
    assert manager.ready()
    manager._close_process(kill=True)


def test_assistant_message_triggers_progress_push(tmp_path, monkeypatch):
    requests = []

    class _Response:
        def close(self):
            pass

    class _Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return _Response()

    monkeypatch.setattr(shim.urllib.request, "build_opener", lambda _handler: _Opener())
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes", progress_token="abc123")
    manager._close_process(kill=True)

    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == (
        "http://monolith.monolith.svc.cluster.local:8091/ingest/progress"
    )
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer abc123"
    payload = json.loads(request.data)
    assert payload["partial_text"] == "Assistant progress."
    assert isinstance(payload["activities"], list)
    assert len(payload["activities"]) == 4
    assert payload["activities"][0] == {
        "type": "tool_use",
        "name": "Read",
        "input": {"file_path": "a.txt"},
    }
    assert timeout == 2


def test_delta_accumulation_folds_without_double_count(tmp_path, monkeypatch):
    """Deltas before and after a complete message do not duplicate text."""
    monkeypatch.setenv("FAKE_DELTAS", "1")
    pushes = []

    class FakePusher:
        def __init__(self, _token):
            pass

        def push(self, text, activities):
            pushes.append((text, activities))

        def stop(self):
            pass

    monkeypatch.setattr(shim, "_ProgressPusher", FakePusher)
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes", progress_token="abc123")
    manager._close_process(kill=True)

    assert "Hello world more" in [text for text, _activities in pushes]
    assert all("Hello Hello world" not in text for text, _activities in pushes)


def test_claude_spawn_includes_include_partial_messages(tmp_path, monkeypatch):
    """Claude CLI spawn includes the partial message stream flag."""
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes")
    assert "--include-partial-messages" in json.loads(args_path.read_text())
    manager._close_process(kill=True)


def test_claude_system_prompt_is_composed_at_spawn(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes", system_prompt="CALLER-MARKER")
    argv = json.loads(args_path.read_text())
    value = argv[argv.index("--append-system-prompt") + 1]
    assert "disposable Firecracker microVM" in value
    assert "CALLER-MARKER" in value
    manager._close_process(kill=True)


def test_claude_prewarm_adoption_respawns_for_system_prompt(tmp_path, monkeypatch):
    manager = _parked_claude(tmp_path)
    calls = []

    def respawn(session_id=None, **kwargs):
        calls.append({"session_id": session_id, **kwargs})
        manager.process = _FakeLiveProcess()
        manager._process_workspace = manager.workspace
        manager.system_prompt = kwargs.get("system_prompt")

    monkeypatch.setattr(manager, "_close_process", lambda **_kwargs: None)
    monkeypatch.setattr(manager, "_spawn", respawn)
    monkeypatch.setattr(
        manager,
        "_read_output",
        lambda _process, _timeout: json.dumps(
            {
                "type": "result",
                "result": "ok",
                "terminal_reason": "end_turn",
                "session_id": "sid",
            }
        ).encode(),
    )
    monkeypatch.setattr(manager, "_parse_line", json.loads)
    monkeypatch.setattr(manager, "ready", lambda: True)

    manager.turn("hello", session_id="sid", system_prompt="CALLER-MARKER")
    assert calls[0]["system_prompt"] == "CALLER-MARKER"


def test_codex_parked_prewarm_adopts_without_respawn(tmp_path, monkeypatch, capsys):
    """A prewarmed app-server serves the first turn with no second spawn.

    This is the codex half of #4423: prewarm runs _spawn() and nothing else,
    the first turn binds a thread via thread/start, and cli_ready reports
    adopt instead of lazy_spawn.
    """
    manager = _codex_manager(tmp_path, monkeypatch)
    manager._spawn()
    parked_process = manager.process
    assert manager.session_id is None

    record = manager.turn("first", model="luna")

    assert manager.process is parked_process
    assert manager.session_id == "codex-thread"
    assert record["session_id"] == "codex-thread"
    spawns = [
        json.loads(line)
        for line in (tmp_path / "codex-args.jsonl").read_text().splitlines()
    ]
    assert spawns == [["app-server"]]
    requests = [
        json.loads(line)
        for line in (tmp_path / "codex-rpc.jsonl").read_text().splitlines()
    ]
    methods = [request.get("method") for request in requests]
    assert methods.count("initialize") == 1
    assert methods.count("thread/start") == 1
    assert "thread/resume" not in methods
    err = capsys.readouterr().err
    assert "phase=cli_ready path=adopt ms=" in err
    assert "path=lazy_spawn" not in err
    manager._close_process()


def test_codex_parked_prewarm_resumes_bound_session(tmp_path, monkeypatch):
    """Late-bound identity (#4358) on a prewarmed server: resume, no conflict."""
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("first", model="luna")
    session_id = manager.session_id
    # Emulate the post-relight state: a fresh prewarmed server that has not
    # bound any thread yet, while the caller asks for its old one.
    manager._close_process(kill=True)
    manager._spawn()
    parked_process = manager.process
    manager.session_id = None

    manager.turn("second", session_id=session_id, model="luna")

    assert manager.process is parked_process
    assert manager.session_id == session_id
    requests = [
        json.loads(line)
        for line in (tmp_path / "codex-rpc.jsonl").read_text().splitlines()
    ]
    assert (
        sum(1 for request in requests if request.get("method") == "thread/resume") == 1
    )
    manager._close_process()


def test_pi_parked_prewarm_serves_first_turn_without_respawn(
    tmp_path, monkeypatch, capsys
):
    """The pi half of #4423: a parked rpc process answers the first prompt.

    Prewarm spawns pi on the default model with no caller system prompt; the
    first turn reuses that process. The fake pi reports a startup session id,
    so this also pins that prewarm clears it: a parked process owns no caller
    session until its first prompt, exactly like the claude park.
    """
    manager = _pi_manager(tmp_path, monkeypatch)
    manager._spawn(shim.DEFAULT_PI_MODEL, system_prompt=None)
    assert manager.session_id is not None
    manager.session_id = None
    parked_process = manager.process

    record = manager.turn("hello", model="qwen")

    assert manager.process is parked_process
    assert record["voice"] == "Pi completed the work."
    spawns = [
        json.loads(line)
        for line in (tmp_path / "pi-args.jsonl").read_text().splitlines()
    ]
    assert len(spawns) == 1
    err = capsys.readouterr().err
    # A fresh turn has no caller session to bind, so the process is reused;
    # the point is that it is NOT a lazy_spawn.
    assert "phase=cli_ready path=reuse ms=" in err
    assert "path=lazy_spawn" not in err
    manager._close_process()


def test_pi_parked_prewarm_binds_caller_session_without_conflict(
    tmp_path, monkeypatch, capsys
):
    """A resume against a parked pi switches sessions instead of conflicting."""
    manager = _pi_manager(tmp_path, monkeypatch)
    manager._spawn(shim.DEFAULT_PI_MODEL, system_prompt=None)
    manager.session_id = None
    parked_process = manager.process

    manager.turn("resume", session_id="pi-session", model="qwen")

    assert manager.process is parked_process
    requests = [
        json.loads(line)
        for line in (tmp_path / "pi-rpc.jsonl").read_text().splitlines()
    ]
    assert (
        sum(1 for request in requests if request.get("type") == "switch_session") == 1
    )
    err = capsys.readouterr().err
    assert "phase=cli_ready path=adopt ms=" in err
    manager._close_process()


def test_malformed_assistant_events_are_skipped(tmp_path, monkeypatch):
    """Malformed assistant shapes do not abort the turn or poison the stream."""
    monkeypatch.setenv("FAKE_MALFORMED", "1")
    manager = _manager(tmp_path, monkeypatch)
    record = manager.turn("make changes", progress_token="abc123")
    assert record["terminal_reason"] == "end_turn"
    manager._close_process(kill=True)


def test_no_progress_push_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shim.urllib.request,
        "build_opener",
        lambda _handler: pytest.fail("progress push should be disabled"),
    )
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes")
    manager._close_process(kill=True)


def test_progress_pusher_uses_egress_proxy_and_swallows_errors(monkeypatch):
    handlers = []

    def proxy_handler(proxies):
        handlers.append(proxies)
        return object()

    class _Opener:
        def open(self, _request, timeout):
            assert timeout == 2
            raise ConnectionRefusedError("egress unavailable")

    monkeypatch.setattr(shim.urllib.request, "ProxyHandler", proxy_handler)
    monkeypatch.setattr(shim.urllib.request, "build_opener", lambda _handler: _Opener())
    pusher = shim._ProgressPusher("abc123")
    pusher.push("text")
    pusher.stop()
    assert handlers == [
        {"http": "http://%s:%s" % (shim.EGRESS_LOCALHOST, shim.DEFAULT_EGRESS_PORT)}
    ]


def test_progress_pusher_collapses_rapid_events_to_latest():
    started = threading.Event()
    release = threading.Event()
    pushed = []
    pusher = shim._ProgressPusher("abc123")

    def delayed_push():
        started.set()
        release.wait(timeout=1)
        pushed.append(pusher.latest_text)

    pusher._do_push = delayed_push
    pusher.push("first")
    assert started.wait(timeout=1)
    pusher.push("second")
    release.set()
    pusher.stop()

    assert pushed == ["second"]


def test_compose_system_prompt():
    assert shim.compose_system_prompt(None) == shim.SANDBOX_PROMPT
    assert shim.compose_system_prompt("  ") == shim.SANDBOX_PROMPT
    composed = shim.compose_system_prompt("X")
    assert composed.startswith(shim.SANDBOX_PROMPT + "\n")
    assert "X" in composed


def test_system_prompt_validation_and_forwarding():
    class _Headers:
        def __init__(self, length):
            self.length = length

        def get(self, name, default=None):
            return str(self.length) if name == "Content-Length" else default

    class _Manager:
        def __init__(self):
            self.calls = []

        def turn(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"result": "ok"}

    def post(payload, manager):
        handler = object.__new__(shim.RequestHandler)
        raw = json.dumps(payload).encode("utf-8")
        handler.path = shim.TURN_PATH
        handler.headers = _Headers(len(raw))
        handler.rfile = io.BytesIO(raw)
        responses = []
        handler._send = lambda status, value: responses.append((status, value))
        handler.manager = manager
        shim.RequestHandler.do_POST(handler)
        return responses

    manager = _Manager()
    assert post({"message": "hello", "progress_token": "  abc123  "}, manager) == [
        (200, {"result": "ok"})
    ]
    assert manager.calls[0][1] == {"progress_token": "abc123"}

    manager = _Manager()
    assert post({"message": "hello", "system_prompt": "  CALLER  "}, manager) == [
        (200, {"result": "ok"})
    ]
    assert manager.calls[0][1] == {"system_prompt": "CALLER"}

    manager = _Manager()
    assert post({"message": "hello"}, manager) == [(200, {"result": "ok"})]
    assert manager.calls[0][1] == {}

    manager = _Manager()
    assert post({"message": "hello", "thinking": "high"}, manager) == [
        (200, {"result": "ok"})
    ]
    assert manager.calls[0][1] == {"thinking": "high"}

    for token in ("", 123):
        manager = _Manager()
        responses = post({"message": "hello", "system_prompt": token}, manager)
        assert responses == [
            (400, {"error": "system_prompt must be a non-empty string"})
        ]
        assert manager.calls == []
    for token in ("", 123):
        manager = _Manager()
        responses = post({"message": "hello", "progress_token": token}, manager)
        assert responses == [
            (400, {"error": "progress_token must be a non-empty string"})
        ]
        assert manager.calls == []

    manager = _Manager()
    responses = post({"message": "hello", "thinking": "bogus"}, manager)
    assert responses == [
        (
            400,
            {
                "error": "thinking must be a bool or one of %s"
                % (shim.PI_THINKING_LEVELS,)
            },
        )
    ]
    assert manager.calls == []


def test_process_manager_forwards_adapter_specific_turn_options(monkeypatch):
    """ProcessManager.turn forwards options only to adapters that accept them."""
    manager = _new_process_manager()
    manager._hydration_error = None
    manager._hydration_status = None
    monkeypatch.setattr(shim, "_sync_session_volume", lambda: None)
    calls = []

    class FakeAdapter:
        def turn(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"result": "ok"}

    manager.claude = FakeAdapter()
    manager.codex = FakeAdapter()
    manager.pi = FakeAdapter()

    manager.turn(
        "msg",
        session_id="s1",
        model=None,
        progress_token="token123",
        system_prompt="CALLER-MARKER",
    )
    assert len(calls) == 1
    assert calls[0][1].get("progress_token") == "token123"
    assert calls[0][1].get("system_prompt") == "CALLER-MARKER"

    calls.clear()
    manager.turn("msg", session_id="s1", model=None)
    assert len(calls) == 1
    assert "progress_token" not in calls[0][1]

    calls.clear()
    manager.turn("msg", session_id="s1", model="qwen", thinking="high")
    assert len(calls) == 1
    assert calls[0][1].get("thinking") == "high"

    calls.clear()
    manager.turn("msg", session_id="s1", model=None, thinking="high")
    assert len(calls) == 1
    assert "thinking" not in calls[0][1]


def test_throttle_allows_push_after_0_2_seconds(monkeypatch):
    """After 0.2 seconds, the next push fires."""
    current_time = [0.0]
    monkeypatch.setattr(shim.time, "monotonic", lambda: current_time[0])
    requests = []

    def fake_opener_open(request, timeout):
        requests.append(request)

        class FakeResp:
            def close(self):
                pass

        return FakeResp()

    class FakeOpener:
        open = staticmethod(fake_opener_open)

    monkeypatch.setattr(
        shim.urllib.request, "build_opener", lambda _handler: FakeOpener()
    )
    pusher = shim._ProgressPusher("token")
    pusher.push("text1")
    pusher.stop()
    assert len(requests) == 1

    current_time[0] = 0.25
    pusher.push("text2")
    pusher.stop()
    assert len(requests) == 2


def test_throttle_blocks_push_within_0_2_seconds(monkeypatch):
    """Within 0.2 seconds of the last push, the next push does not fire."""
    current_time = [0.0]
    monkeypatch.setattr(shim.time, "monotonic", lambda: current_time[0])
    requests = []

    def fake_opener_open(request, timeout):
        requests.append(request)

        class FakeResp:
            def close(self):
                pass

        return FakeResp()

    class FakeOpener:
        open = staticmethod(fake_opener_open)

    monkeypatch.setattr(
        shim.urllib.request, "build_opener", lambda _handler: FakeOpener()
    )
    pusher = shim._ProgressPusher("token")
    pusher.push("text1")
    pusher.stop()
    assert len(requests) == 1

    current_time[0] = 0.1
    pusher.push("text2")
    pusher.stop()
    assert len(requests) == 1


def test_pusher_throttle_0_2_seconds_with_drain(monkeypatch):
    """Drain re-fires after 0.2 seconds and sends the final slot state."""
    requests = []
    current_time = [0.0]
    monkeypatch.setattr(shim.time, "monotonic", lambda: current_time[0])
    monkeypatch.setattr(
        shim.time,
        "sleep",
        lambda duration: current_time.__setitem__(0, current_time[0] + duration),
    )
    pusher = shim._ProgressPusher("token")

    class FakeResponse:
        def close(self):
            pass

    class FakeOpener:
        def open(self, request, timeout):
            requests.append(
                (current_time[0], json.loads(request.data.decode())["partial_text"])
            )
            if len(requests) == 1:
                pusher.latest_text = "text2"
            return FakeResponse()

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def is_alive(self):
            return False

        def start(self):
            self.target()

    monkeypatch.setattr(
        shim.urllib.request, "build_opener", lambda _handler: FakeOpener()
    )
    monkeypatch.setattr(shim.threading, "Thread", ImmediateThread)
    pusher.push("text1", [])
    assert [text for _, text in requests] == ["text1", "text2"]
    assert requests[1][0] >= requests[0][0] + 0.2


def test_payload_trimmed_to_byte_budget(monkeypatch):
    """Oversized progress payloads are trimmed below the ingest limit."""
    captured_payloads = []

    class FakeResponse:
        def close(self):
            pass

    class FakeOpener:
        def open(self, request, timeout):
            captured_payloads.append(request.data)
            return FakeResponse()

    monkeypatch.setattr(
        shim.urllib.request, "build_opener", lambda _handler: FakeOpener()
    )
    pusher = shim._ProgressPusher("token")
    pusher.push(
        "x" * 500000,
        [{"index": i, "data": "y" * 1000} for i in range(300)],
    )
    pusher.stop()

    assert len(captured_payloads) == 1
    payload = json.loads(captured_payloads[0].decode())
    assert len(captured_payloads[0]) < 262144
    assert len(payload["activities"]) < 300


def test_stream_event_reuses_cached_activities(tmp_path, monkeypatch):
    """Text deltas do not recompute activities for every stream event."""
    monkeypatch.setenv("FAKE_DELTAS", "1")
    calls = {"extract": 0}
    original_extract = shim.activity_from_events

    def counting_extract(events):
        calls["extract"] += 1
        return original_extract(events)

    monkeypatch.setattr(shim, "activity_from_events", counting_extract)
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes", progress_token="abc123")
    manager._close_process(kill=True)
    assert calls["extract"] <= 3


def test_activities_sent_in_payload_capped_to_300(tmp_path, monkeypatch):
    """Progress activities are capped to the last 300 entries."""
    captured = []

    class FakePusher:
        def __init__(self, _token):
            pass

        def push(self, _text, activities):
            captured.append(activities)

        def stop(self):
            pass

    monkeypatch.setattr(shim, "_ProgressPusher", FakePusher)
    monkeypatch.setattr(
        shim,
        "activity_from_events",
        lambda _events: [{"index": index} for index in range(400)],
    )
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes", progress_token="abc123")
    manager._close_process(kill=True)

    assert captured
    assert all(len(activities) <= 300 for activities in captured)
    assert captured[-1][0] == {"index": 100}


def test_push_thread_creation_failure_swallowed(monkeypatch):
    """Thread startup failure is swallowed; the turn can continue."""

    def fake_thread_init(*args, **kwargs):
        raise RuntimeError("thread creation failed")

    monkeypatch.setattr(shim.threading, "Thread", fake_thread_init)
    shim._ProgressPusher("token").push("text")


def test_large_accumulation_capped_to_tail(monkeypatch):
    """Text larger than 65536 bytes is capped to its tail in the payload."""
    captured_payloads = []

    def fake_opener_open(request, timeout):
        captured_payloads.append(request.data)

        class FakeResp:
            def close(self):
                pass

        return FakeResp()

    class FakeOpener:
        open = staticmethod(fake_opener_open)

    monkeypatch.setattr(
        shim.urllib.request, "build_opener", lambda _handler: FakeOpener()
    )
    pusher = shim._ProgressPusher("token")
    large_text = "x" * 100000
    pusher.push(large_text)
    pusher.stop()

    assert len(captured_payloads) == 1
    payload = json.loads(captured_payloads[0].decode())
    assert payload["partial_text"] == large_text[-65536:]
    assert len(payload["partial_text"]) == 65536


def test_accumulation_order_preserves_assistant_text(monkeypatch):
    """Sequential pushes preserve the accumulated assistant text order."""
    current_time = [0.0]
    monkeypatch.setattr(shim.time, "monotonic", lambda: current_time[0])
    payloads = []

    def fake_opener_open(request, timeout):
        payloads.append(json.loads(request.data.decode())["partial_text"])

        class FakeResp:
            def close(self):
                pass

        return FakeResp()

    class FakeOpener:
        open = staticmethod(fake_opener_open)

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def is_alive(self):
            return False

        def start(self):
            self.target()

    monkeypatch.setattr(
        shim.urllib.request, "build_opener", lambda _handler: FakeOpener()
    )
    monkeypatch.setattr(shim.threading, "Thread", ImmediateThread)
    pusher = shim._ProgressPusher("token")
    for text in ("block1", "block1block2", "block1block2block3"):
        pusher.push(text)
        current_time[0] += 1.0

    assert payloads == ["block1", "block1block2", "block1block2block3"]


def test_claude_model_argv_and_mid_session_switch_resumes(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first", model="opus")
    assert (
        json.loads(args_path.read_text())[
            json.loads(args_path.read_text()).index("--model") + 1
        ]
        == "opus"
    )
    manager.turn("second", session_id="init-sid", model="fable")
    args = json.loads(args_path.read_text())
    assert args[args.index("--resume") + 1] == "init-sid"
    assert args[args.index("--model") + 1] == "claude-fable-5"
    manager._close_process(kill=True)


def test_claude_model_none_keeps_legacy_argv(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    assert "--model" not in json.loads(args_path.read_text())
    manager._close_process(kill=True)


def test_manager_passes_claude_model_to_adapter():
    manager = _new_process_manager()

    class Claude:
        def turn(self, *args):
            return args

    manager.claude = Claude()
    manager.codex = object()
    manager.pi = object()
    assert manager.turn("hello", "sid", "fable") == ("hello", "sid", "fable")


def test_first_turn_is_sent_before_delayed_cli_init_and_only_once(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    executable.write_text(FAKE_CLI_INIT_AFTER_INPUT)
    os.chmod(executable, 0o755)
    lines_path = tmp_path / "lines.jsonl"
    monkeypatch.setenv("FAKE_LINES", str(lines_path))
    monkeypatch.setenv("EMBER_INIT_READ_TIMEOUT", "2")
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    first = manager.turn("first")
    assert first["terminal_reason"] == "end_turn"
    assert manager.session_id == "delayed-sid"
    assert json.loads(lines_path.read_text()) == {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "first"}],
        },
    }

    second = manager.turn("second")
    assert second["terminal_reason"] == "end_turn"
    assert manager.process.poll() is None
    assert [
        json.loads(line)["message"]["content"][0]["text"]
        for line in lines_path.read_text().splitlines()
    ] == ["first", "second"]
    manager._close_process(kill=True)


def test_claude_prewarm_parks_without_init_and_leaves_process_alive(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EMBER_PARK_GRACE_SECONDS", "0.05")
    claude = _manager(tmp_path, monkeypatch)
    process_manager = _new_process_manager()
    process_manager._prewarm_clis = ("claude",)
    process_manager._prewarm_complete = False
    process_manager.fatal_error = None
    process_manager.claude = claude

    class IdleAdapter:
        process = None
        session_id = None
        turn_lock = threading.Lock()

        def ready(self):
            return True

    process_manager.codex = IdleAdapter()
    process_manager.pi = IdleAdapter()
    process_manager._close_process = lambda kill=False: claude._close_process(kill=kill)
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)

    process_manager.prewarm()

    assert process_manager.fatal_error is None
    assert process_manager._prewarm_complete
    assert claude.process is not None
    assert claude.process.poll() is None
    assert claude.init_event is None
    assert claude.session_id is None
    claude._close_process(kill=True)


def test_first_turn_after_park_ignores_init_and_binds_result_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EMBER_PARK_GRACE_SECONDS", "0.05")
    manager = _manager(tmp_path, monkeypatch)
    manager._spawn(first_message=None)
    parked_process = manager.process
    parked_queue = manager._stdout_queue

    record = manager.turn("first")

    assert manager.process is parked_process
    assert manager._stdout_queue is parked_queue
    assert manager.init_event is None
    assert manager.session_id == "sid-1"
    assert record["terminal_reason"] == "end_turn"
    manager._close_process(kill=True)


def _write_chatty_no_init_cli(tmp_path):
    executable = tmp_path / "chatty-no-init-cli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    sys.exit(0)\n"
        "sys.stdin.readline()\n"
        "sequence = 0\n"
        "while True:\n"
        "    print(json.dumps({'type': 'progress', 'message': 'still starting',\n"
        "                      'sequence': sequence}), flush=True)\n"
        "    sequence += 1\n"
        "    time.sleep(0.01)\n"
    )
    os.chmod(executable, 0o755)
    return executable


def test_chatty_message_spawn_uses_total_init_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("EMBER_INIT_READ_TIMEOUT", "0.2")
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _write_chatty_no_init_cli(tmp_path)
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    with pytest.raises(shim.StartupError) as exc_info:
        manager.turn("test")

    error_msg = str(exc_info.value)
    assert "after 0.2 seconds" in error_msg
    assert "Parsed events:" in error_msg
    assert "still starting" in error_msg


def test_message_less_spawn_death_during_park_grace_is_startup_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("EMBER_PARK_GRACE_SECONDS", "0.1")
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "dead-park-cli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    sys.exit(0)\n"
        "print('park startup failed', file=sys.stderr, flush=True)\n"
        "sys.exit(7)\n"
    )
    os.chmod(executable, 0o755)
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    with pytest.raises(shim.StartupError) as exc_info:
        manager._spawn(first_message=None)

    error_msg = str(exc_info.value)
    assert "park grace, exit code 7" in error_msg
    assert "CLI stderr:" in error_msg
    assert "park startup failed" in error_msg
    assert manager.process is None


def test_park_grace_env_override_and_fallback(monkeypatch):
    monkeypatch.delenv(shim.PARK_GRACE_SECONDS_ENV, raising=False)
    assert shim._read_park_grace_seconds() == 15.0
    monkeypatch.setenv(shim.PARK_GRACE_SECONDS_ENV, "0.25")
    assert shim._read_park_grace_seconds() == 0.25
    monkeypatch.setenv(shim.PARK_GRACE_SECONDS_ENV, "not a number")
    assert shim._read_park_grace_seconds() == 15.0


def test_voice_falls_back_to_first_sentence(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.turn("fallback")["voice"] == "First sentence."
    manager._close_process(kill=True)


def test_turn_reports_permission_denials(tmp_path, monkeypatch):
    """Verify permission denials are reported, not swallowed."""
    manager = _manager(tmp_path, monkeypatch)
    record = manager.turn("permission denied")
    assert record["permission_denials"] == [
        {
            "tool_name": "Write",
            "tool_use_id": "toolu_test",
            "reason": "requires_approval",
        }
    ]
    manager._close_process(kill=True)


def test_truncated_jsonl_after_result_is_ignored(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.turn("truncated")["terminal_reason"] == "end_turn"
    manager._close_process(kill=True)


def test_api_key_source_assertion(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, api_key="api_key")
    with pytest.raises(shim.StartupError, match="apiKeySource must be none"):
        manager.turn("hello")
    assert not manager.ready()


def test_configure_git_uses_decoded_boot_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # These are the values guest-init receives from decoded ember.env boot args.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Boot User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "boot@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), "unused")
    manager._configure_git()
    assert (
        subprocess.check_output(
            ["git", "config", "--global", "user.name"], text=True
        ).strip()
        == "Boot User"
    )
    assert (
        subprocess.check_output(
            ["git", "config", "--global", "user.email"], text=True
        ).strip()
        == "boot@example.invalid"
    )


def test_workspace_volume_helper_invokes_guest_init_once_per_call(monkeypatch):
    calls = []
    monkeypatch.setattr(shim.os.path, "exists", lambda path: True)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    shim.ensure_workspace_volume()
    shim.ensure_workspace_volume()
    assert [call[0] for call in calls] == [
        [shim.GUEST_INIT_PATH, "--ensure-workspace-volume", "--device", "/dev/vdb"],
        [shim.GUEST_INIT_PATH, "--ensure-workspace-volume", "--device", "/dev/vdb"],
    ]
    assert all(call[1]["check"] for call in calls)


def test_activity_ignores_malformed_messages_and_bounds_tool_input():
    activity = shim.activity_from_events(
        [
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": "not an object"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"large": "x" * (shim.MAX_TOOL_INPUT_BYTES + 1)},
                        }
                    ]
                },
            },
        ]
    )
    assert activity == [
        {
            "type": "tool_use",
            "name": "Read",
            "input": "[omitted: tool input exceeds %d bytes]"
            % shim.MAX_TOOL_INPUT_BYTES,
        }
    ]


def test_child_reaper_preserves_managed_exit_status_and_reaps_unmanaged_child():
    previous_handler = signal.getsignal(signal.SIGCHLD)
    shim.install_child_reaper()
    try:
        managed = subprocess.Popen(["bash", "-c", "sleep 0.05; exit 7"])
        shim._managed_child_pids.add(managed.pid)
        assert managed.wait() == 7
        shim._managed_child_pids.discard(managed.pid)

        orphan = subprocess.Popen(["bash", "-c", "exit 0"])
        deadline = time.time() + 2
        while time.time() < deadline:
            shim._reap_orphans()
            try:
                info = os.waitid(
                    os.P_PID, orphan.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError:
                break
            assert info is None
            time.sleep(0.01)
        else:
            pytest.fail("unmanaged child was not reaped")
    finally:
        signal.signal(signal.SIGCHLD, previous_handler)


def test_interrupt_waits_for_clean_exit(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    errors = []

    def run_turn():
        try:
            manager.turn("block")
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run_turn)
    thread.start()
    deadline = time.time() + 2
    while manager.process is None and time.time() < deadline:
        time.sleep(0.01)
    outcome = manager.interrupt(timeout=1)
    thread.join(2)
    assert outcome == {
        "terminal_reason": "user_interrupt",
        "killed": False,
        "timeout": False,
    }
    assert manager.process is None
    assert errors


def test_resume_argument_is_used_on_new_process(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    first_args = json.loads(args_path.read_text())
    assert "--resume" not in first_args
    verbose_index = first_args.index("--verbose")
    assert first_args[verbose_index : verbose_index + 3] == [
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]
    manager._close_process(kill=True)
    manager.turn("second", session_id="init-sid")
    second_args = json.loads(args_path.read_text())
    assert second_args[second_args.index("--resume") + 1] == "init-sid"
    manager._close_process(kill=True)


def test_permission_mode_can_be_overridden(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    monkeypatch.setenv(shim.PERMISSION_MODE_ENV, "default")
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    args = json.loads(args_path.read_text())
    assert args[args.index("--permission-mode") + 1] == "default"
    manager._close_process(kill=True)


def test_spawn_sets_egress_proxy_environment(tmp_path, monkeypatch):
    env_path = tmp_path / "egress-env.json"
    monkeypatch.setenv("FAKE_EGRESS_ENV", str(env_path))
    monkeypatch.setenv(shim.EGRESS_PORT_ENV, "1042")
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    assert json.loads(env_path.read_text()) == {
        "HTTPS_PROXY": "http://127.0.0.1:1042",
        "HTTP_PROXY": "http://127.0.0.1:1042",
        "NO_PROXY": "127.0.0.1,localhost",
        # Lowercase is not redundant. curl honours http_proxy in lowercase ONLY,
        # so a guest tool that curls an http:// URL with just HTTP_PROXY set
        # bypasses the egress lane and dies resolving the host in a guest that
        # has no NIC. See shim.egress_proxy_env.
        "https_proxy": "http://127.0.0.1:1042",
        "http_proxy": "http://127.0.0.1:1042",
        "no_proxy": "127.0.0.1,localhost",
    }
    manager._close_process(kill=True)


@pytest.mark.parametrize("manager_factory", (_codex_manager, _pi_manager))
def test_cli_child_env_points_plain_http_at_the_egress_lane(
    manager_factory, tmp_path, monkeypatch
):
    """Every adapter's children must reach the lane over http:// as well as https://.

    Pi's web_search queries the in-cluster SearXNG endpoint over plain HTTP with
    curl, which reads only the lowercase name. With uppercase alone the request
    left the proxy behind, found no resolver in a NIC-less guest, and surfaced as
    a SearXNG outage rather than a routing gap.
    """
    monkeypatch.setenv(shim.EGRESS_PORT_ENV, "1042")
    child_env = manager_factory(tmp_path, monkeypatch)._child_env()
    assert child_env["http_proxy"] == "http://127.0.0.1:1042"
    assert child_env["https_proxy"] == "http://127.0.0.1:1042"
    assert child_env["no_proxy"] == "127.0.0.1,localhost"
    assert child_env["HTTP_PROXY"] == child_env["http_proxy"]
    assert child_env["HTTPS_PROXY"] == child_env["https_proxy"]
    assert child_env["NO_PROXY"] == child_env["no_proxy"]


def test_egress_forwarder_opens_one_vsock_connection_per_accept(monkeypatch):
    forwarder = shim.VsockEgressForwarder(port=0)
    original_socket = socket.socket
    vsock_peer, vsock_forwarder = socket.socketpair()
    connected = []
    fake_vsocks = []

    class FakeVsock:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def connect(self, address):
            connected.append(address)

        def recv(self, size):
            return vsock_forwarder.recv(size)

        def sendall(self, data):
            return vsock_forwarder.sendall(data)

        def shutdown(self, how):
            return vsock_forwarder.shutdown(how)

        def close(self):
            return vsock_forwarder.close()

    def socket_factory(family, *args, **kwargs):
        if family == shim.VSOCK_ADDRESS_FAMILY:
            fake_vsock = FakeVsock()
            fake_vsocks.append(fake_vsock)
            return fake_vsock
        return original_socket(family, *args, **kwargs)

    monkeypatch.setattr(shim.socket, "socket", socket_factory)
    forwarder.listen()
    client = socket.create_connection((shim.EGRESS_LOCALHOST, forwarder.port))
    try:
        # The CLI opens an HTTPS_PROXY connection with CONNECT; the host lane is
        # told the destination as a bare "host:port" line, and only then do raw
        # bytes flow.
        client.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
        preamble = b"api.anthropic.com:443\n"
        assert vsock_peer.recv(len(preamble)) == preamble
        established = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        assert client.recv(len(established)) == established
        client.sendall(b"request")
        assert vsock_peer.recv(7) == b"request"
        vsock_peer.sendall(b"response")
        assert client.recv(8) == b"response"
        assert connected == [(shim.VSOCK_EGRESS_CID, shim.VSOCK_EGRESS_PORT)]
        assert fake_vsocks[0].timeouts == [
            shim.EGRESS_VSOCK_CONNECT_TIMEOUT_SECONDS,
            None,
        ]
    finally:
        client.close()
        vsock_peer.close()
        forwarder.close()


def test_egress_vsock_connect_constants_pinned():
    assert shim.EGRESS_VSOCK_CONNECT_TIMEOUT_SECONDS == 5.0
    assert shim.EGRESS_VSOCK_CONNECT_ATTEMPTS == 3


def test_egress_forwarder_retries_vsock_connect(monkeypatch):
    forwarder = shim.VsockEgressForwarder(port=0)
    original_socket = socket.socket
    vsock_peer, vsock_forwarder = socket.socketpair()
    attempts = []
    fake_vsocks = []

    class FakeVsock:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def connect(self, address):
            attempts.append(address)
            if len(attempts) < 3:
                raise OSError("wedged lane")

        def recv(self, size):
            return vsock_forwarder.recv(size)

        def sendall(self, data):
            return vsock_forwarder.sendall(data)

        def shutdown(self, how):
            return vsock_forwarder.shutdown(how)

        def close(self):
            pass

    def socket_factory(family, *args, **kwargs):
        if family == shim.VSOCK_ADDRESS_FAMILY:
            fake_vsock = FakeVsock()
            fake_vsocks.append(fake_vsock)
            return fake_vsock
        return original_socket(family, *args, **kwargs)

    monkeypatch.setattr(shim.socket, "socket", socket_factory)
    monkeypatch.setattr(shim.time, "sleep", lambda _: None)
    forwarder.listen()
    client = socket.create_connection((shim.EGRESS_LOCALHOST, forwarder.port))
    try:
        client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\nrequest")
        preamble = b"example.com:443\n"
        assert vsock_peer.recv(len(preamble)) == preamble
        established = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        assert client.recv(len(established)) == established
        assert vsock_peer.recv(7) == b"request"
        vsock_peer.sendall(b"response")
        assert client.recv(8) == b"response"
        assert len(attempts) == 3
        assert fake_vsocks[-1].timeouts == [
            shim.EGRESS_VSOCK_CONNECT_TIMEOUT_SECONDS,
            None,
        ]
    finally:
        client.close()
        vsock_peer.close()
        forwarder.close()


def test_egress_forwarder_failed_vsock_connect_closes_client(monkeypatch, capsys):
    forwarder = shim.VsockEgressForwarder(port=0)
    original_socket = socket.socket
    attempts = []
    fake_vsocks = []

    class FakeVsock:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def connect(self, address):
            attempts.append(address)
            raise OSError("wedged lane")

        def close(self):
            pass

    def socket_factory(family, *args, **kwargs):
        if family == shim.VSOCK_ADDRESS_FAMILY:
            fake_vsock = FakeVsock()
            fake_vsocks.append(fake_vsock)
            return fake_vsock
        return original_socket(family, *args, **kwargs)

    monkeypatch.setattr(shim.socket, "socket", socket_factory)
    monkeypatch.setattr(shim.time, "sleep", lambda _: None)
    forwarder.listen()
    client = socket.create_connection((shim.EGRESS_LOCALHOST, forwarder.port))
    try:
        client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        client.settimeout(2)
        assert client.recv(4096) == b""
        assert len(attempts) == shim.EGRESS_VSOCK_CONNECT_ATTEMPTS
        assert "egress vsock connect failed" in capsys.readouterr().err
        assert all(
            fake_vsock.timeouts == [shim.EGRESS_VSOCK_CONNECT_TIMEOUT_SECONDS]
            for fake_vsock in fake_vsocks
        )
    finally:
        client.close()
        forwarder.close()


def test_git_proxy_helper_handshake_timeout(tmp_path, monkeypatch):
    helper_path = tmp_path / "ember-git-proxy"
    monkeypatch.setattr(shim, "GIT_PROXY_PATH", str(helper_path))
    shim._write_git_proxy_helper()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((shim.EGRESS_LOCALHOST, 0))
    listener.listen()
    accepted = []

    def accept_connection():
        connection, _ = listener.accept()
        accepted.append(connection)

    accept_thread = threading.Thread(target=accept_connection, daemon=True)
    accept_thread.start()
    environment = os.environ.copy()
    environment[shim.EGRESS_PORT_ENV] = str(listener.getsockname()[1])
    environment["EMBER_GIT_PROXY_HANDSHAKE_TIMEOUT_SECONDS"] = "1"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(helper_path), "example.com", "9418"],
            env=environment,
            capture_output=True,
            timeout=3,
        )
    finally:
        listener.close()
        accept_thread.join(timeout=1)
        for connection in accepted:
            connection.close()
    assert result.returncode == 1
    captured_err = result.stderr.decode().lower()
    assert "timed out" in captured_err or "timeout" in captured_err
    assert time.monotonic() - started < 3


def test_git_proxy_helper_sets_tcp_nodelay(tmp_path, monkeypatch):
    helper_path = tmp_path / "ember-git-proxy"
    monkeypatch.setattr(shim, "GIT_PROXY_PATH", str(helper_path))
    shim._write_git_proxy_helper()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((shim.EGRESS_LOCALHOST, 0))
    listener.listen()
    accepted = []
    created_connections = []

    def serve_connection():
        connection, _ = listener.accept()
        accepted.append(connection)
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        connection.shutdown(socket.SHUT_WR)

    accept_thread = threading.Thread(target=serve_connection, daemon=True)
    accept_thread.start()
    monkeypatch.setenv(shim.EGRESS_PORT_ENV, str(listener.getsockname()[1]))
    original_create_connection = socket.create_connection

    def create_connection(*args, **kwargs):
        connection = original_create_connection(*args, **kwargs)
        created_connections.append(connection)
        return connection

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(sys, "argv", [str(helper_path), "example.com", "9418"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    try:
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(str(helper_path), run_name="__main__")
    finally:
        listener.close()
        accept_thread.join(timeout=1)
        for connection in accepted:
            connection.close()
    assert exit_info.value.code == 0
    assert len(created_connections) == 1
    assert (
        created_connections[0].getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
    )


def test_git_proxy_helper_blocks_after_handshake_timeout(tmp_path, monkeypatch):
    helper_path = tmp_path / "ember-git-proxy"
    monkeypatch.setattr(shim, "GIT_PROXY_PATH", str(helper_path))
    shim._write_git_proxy_helper()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((shim.EGRESS_LOCALHOST, 0))
    listener.listen()
    accepted = []

    def serve_connection():
        connection, _ = listener.accept()
        accepted.append(connection)
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        time.sleep(1.5)
        connection.sendall(b"late payload")
        connection.shutdown(socket.SHUT_WR)

    accept_thread = threading.Thread(target=serve_connection, daemon=True)
    accept_thread.start()
    environment = os.environ.copy()
    environment[shim.EGRESS_PORT_ENV] = str(listener.getsockname()[1])
    environment["EMBER_GIT_PROXY_HANDSHAKE_TIMEOUT_SECONDS"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, str(helper_path), "example.com", "9418"],
            input=b"",
            env=environment,
            capture_output=True,
            timeout=5,
        )
    finally:
        listener.close()
        accept_thread.join(timeout=1)
        for connection in accepted:
            connection.close()
    assert result.returncode == 0
    assert result.stdout == b"late payload"


def _read_until(sock, expect, deadline_seconds=2):
    """Accumulate from sock until expect is present, or the deadline expires.

    A single recv() is not a message read. The forwarder writes the destination
    preamble and the replayed request head as two separate sendall() calls
    (shim.py, VsockEgressForwarder: the preamble, then `pending`), and a stream
    socket carries no boundary between them, so one recv() returns whichever
    bytes happen to have arrived. Usually both writes coalesce and a single read
    sees everything; under scheduling pressure the first read returns only the
    preamble, which is what made this suite FLAKY under Bazel retry (#4425,
    observed as `assert b'GET http://example.com/x HTTP/1.1' in
    b'example.com:80\\n'`).

    expect=None means "expect nothing", so this drains until the deadline and
    returns whatever arrived, which is how a caller asserts that the forwarder
    stayed silent.
    """
    chunks = b""
    finished = time.monotonic() + deadline_seconds
    while True:
        if expect is not None and expect in chunks:
            return chunks
        remaining = finished - time.monotonic()
        if remaining <= 0:
            return chunks
        try:
            sock.settimeout(remaining)
            received = sock.recv(4096)
        except (TimeoutError, OSError):
            return chunks
        if not received:
            return chunks
        chunks += received


def _forwarder_exchange(monkeypatch, request_bytes, expect_upstream=None):
    """Drive one connection through the forwarder, returning what the lane saw.

    expect_upstream is the byte string the caller is going to assert on. It is
    passed in rather than inferred so the read stops as soon as the assertion
    could succeed, instead of being a race against whichever bytes arrived first.
    """
    forwarder = shim.VsockEgressForwarder(port=0)
    original_socket = socket.socket
    vsock_peer, vsock_forwarder = socket.socketpair()

    class FakeVsock:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def connect(self, address):
            pass

        def recv(self, size):
            return vsock_forwarder.recv(size)

        def sendall(self, data):
            return vsock_forwarder.sendall(data)

        def shutdown(self, how):
            return vsock_forwarder.shutdown(how)

        def close(self):
            return vsock_forwarder.close()

    def socket_factory(family, *args, **kwargs):
        if family == shim.VSOCK_ADDRESS_FAMILY:
            return FakeVsock()
        return original_socket(family, *args, **kwargs)

    monkeypatch.setattr(shim.socket, "socket", socket_factory)
    forwarder.listen()
    client = socket.create_connection((shim.EGRESS_LOCALHOST, forwarder.port))
    try:
        client.sendall(request_bytes)
        upstream_saw = _read_until(vsock_peer, expect_upstream)
        client.settimeout(2)
        try:
            client_saw = client.recv(4096)
        except (TimeoutError, OSError):
            client_saw = b""
        return upstream_saw, client_saw
    finally:
        client.close()
        vsock_peer.close()
        forwarder.close()


def test_hydration_diagnostics_timeout(capsys, monkeypatch, tmp_path):
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()
    (checkout_dir / "small").write_bytes(b"content")
    monkeypatch.setattr(shim.time, "sleep", lambda seconds: None)
    exc = subprocess.TimeoutExpired(
        cmd=["git", "clone"],
        timeout=300,
        output=b"out",
        stderr=b"Receiving objects: 87%",
    )

    shim._write_hydration_diagnostics(exc, str(checkout_dir))

    captured = capsys.readouterr().err
    assert "Receiving objects: 87%" in captured
    assert "checkout_kb=" in captured
    assert "diskstats=" in captured
    assert (
        "diskstats=unavailable" in captured
        or " vda " in captured
        or " vdb " in captured
    )


def test_hydration_diagnostics_generic_exception(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(shim.time, "sleep", lambda seconds: None)

    shim._write_hydration_diagnostics(
        ValueError("not a subprocess error"), str(tmp_path)
    )

    captured = capsys.readouterr().err
    assert "ValueError" in captured


class _WorkspaceAdapter:
    """Minimal stand-in for the three CLI adapters hydration reassigns."""

    def __init__(self):
        self.workspace = None
        self.requires_git_checkout = False


def _hydration_manager(tmp_path, monkeypatch):
    manager = object.__new__(shim.ProcessManager)
    manager.workspace = str(tmp_path / "workspace")
    os.makedirs(manager.workspace, exist_ok=True)
    manager._hydration_attempts = 0
    manager._hydration_error = None
    manager._checkout_dir = None
    manager._hydration_status = None
    manager.claude = _WorkspaceAdapter()
    manager.codex = _WorkspaceAdapter()
    manager.pi = _WorkspaceAdapter()
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    return manager


def _materialize_checkout(path):
    """Create what a successful clone leaves: a .git with a resolvable HEAD."""
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)
    with open(os.path.join(path, ".git", "HEAD"), "w") as stream:
        stream.write("ref: refs/heads/main\n")


def _install_fake_git(monkeypatch, clones=None, on_validate=None):
    """Fake git where clone materializes a checkout and rev-parse reads it.

    HEAD resolving is keyed on .git/HEAD existing, which is what separates a
    real checkout from the two shapes this fix is about: an empty directory,
    and a .git holding nothing but info/exclude.
    """
    real_run = shim.subprocess.run

    def fake_run(args, **kwargs):
        if not isinstance(args, (list, tuple)):
            return real_run(args, **kwargs)
        args = list(args)
        if "clone" in args:
            destination = args[-1]
            if clones is not None:
                clones.append(destination)
            _materialize_checkout(destination)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if "rev-parse" in args:
            path = args[args.index("-C") + 1]
            resolved = os.path.exists(os.path.join(path, ".git", "HEAD"))
            result = subprocess.CompletedProcess(
                args, 0 if resolved else 128, b"a" * 40 + b"\n", b""
            )
            if resolved and on_validate is not None:
                on_validate(path)
            return result
        return real_run(args, **kwargs)

    monkeypatch.setattr(shim.subprocess, "run", fake_run)


def test_directory_without_a_resolvable_head_is_not_a_usable_checkout(
    tmp_path, monkeypatch
):
    # The turn path recreates <workspace>/src on every turn, so a checkout that
    # went away comes back as an empty directory. isdir accepted it.
    _install_fake_git(monkeypatch)
    checkout = tmp_path / "src"
    checkout.mkdir()

    assert os.path.isdir(checkout)
    assert shim._checkout_is_usable(str(checkout)) is False


def test_git_stub_holding_only_info_exclude_is_not_a_usable_checkout(
    tmp_path, monkeypatch
):
    # The observed artefact on all six guests: .git exists and contains
    # info/exclude and nothing else. No HEAD, no config, no refs, no objects.
    _install_fake_git(monkeypatch)
    checkout = tmp_path / "src"
    (checkout / ".git" / "info").mkdir(parents=True)
    (checkout / ".git" / "info" / "exclude").write_text(".codex/\n.pi/\n")

    assert os.listdir(checkout / ".git") == ["info"]
    assert shim._checkout_is_usable(str(checkout)) is False


def test_repo_less_spawn_still_accepts_a_bare_directory(tmp_path, monkeypatch):
    # A session with no repo legitimately runs in an empty directory, so the
    # strict check must not fire until a turn has named a repo.
    _install_fake_git(monkeypatch)
    workspace = tmp_path / "src"
    workspace.mkdir()

    assert shim._workspace_ready_for_spawn(str(workspace), False) is True
    assert shim._workspace_ready_for_spawn(str(workspace), True) is False


def test_claude_spawn_rejects_an_empty_checkout_on_a_repo_backed_session(
    tmp_path, monkeypatch
):
    _install_fake_git(monkeypatch)
    claude = _manager(tmp_path, monkeypatch)
    claude.requires_git_checkout = True

    with pytest.raises(shim.StartupError, match="workspace does not exist"):
        claude._spawn()


def test_pi_spawn_rejects_the_git_stub_on_a_repo_backed_session(tmp_path, monkeypatch):
    # qwen routes to Pi, and the drainer's failure taxonomy keys on this exact
    # message, so the stub has to raise it rather than start a turn.
    _install_fake_git(monkeypatch)
    checkout = tmp_path / "src"
    (checkout / ".git" / "info").mkdir(parents=True)
    (checkout / ".git" / "info" / "exclude").write_text(".codex/\n.pi/\n")
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    pi = shim.PiProcess(str(checkout), "pi")
    pi.requires_git_checkout = True

    with pytest.raises(shim.StartupError, match="workspace does not exist"):
        pi._spawn("qwen")


def test_hydration_clones_once_then_skips_a_usable_checkout(tmp_path, monkeypatch):
    clones = []
    _install_fake_git(monkeypatch, clones=clones)
    manager = _hydration_manager(tmp_path, monkeypatch)

    manager._hydrate_workspace("jomcgi/homelab", "main")

    checkout = os.path.join(manager.workspace, "src")
    assert clones == [checkout]
    assert manager._hydration_status == "ok"
    assert manager._checkout_dir == checkout
    assert manager.pi.workspace == checkout
    with open(os.path.join(checkout, ".git", "info", "exclude")) as stream:
        assert ".codex/" in stream.read()

    # Re-entry on a checkout that is still there must stay cheap: no re-clone.
    manager._hydrate_workspace("jomcgi/homelab", "main")

    assert clones == [checkout]
    assert manager._hydration_status == "skipped_existing"


def test_hydration_re_runs_when_the_checkout_vanished(tmp_path, monkeypatch):
    clones = []
    _install_fake_git(monkeypatch, clones=clones)
    manager = _hydration_manager(tmp_path, monkeypatch)
    manager._hydrate_workspace("jomcgi/homelab", "main")
    checkout = manager._checkout_dir
    assert manager._hydration_status == "ok"

    # The checkout goes away under a session that already hydrated, and the
    # turn path puts the bare directory back. That empty directory is what the
    # old isdir gate accepted, which is how six drainer jobs ran a whole turn
    # against no source and still recorded last_status ok.
    shutil.rmtree(checkout)
    os.makedirs(checkout)

    manager._hydrate_workspace("jomcgi/homelab", "main")

    assert clones == [checkout, checkout]
    assert manager._hydration_status == "ok"
    assert os.path.exists(os.path.join(checkout, ".git", "HEAD"))


def test_hydration_re_runs_when_the_checkout_is_a_git_stub(tmp_path, monkeypatch):
    clones = []
    _install_fake_git(monkeypatch, clones=clones)
    manager = _hydration_manager(tmp_path, monkeypatch)
    manager._hydrate_workspace("jomcgi/homelab", "main")
    checkout = manager._checkout_dir

    shutil.rmtree(checkout)
    os.makedirs(os.path.join(checkout, ".git", "info"))
    with open(os.path.join(checkout, ".git", "info", "exclude"), "w") as stream:
        stream.write(".codex/\n.pi/\n")

    manager._hydrate_workspace("jomcgi/homelab", "main")

    assert clones == [checkout, checkout]
    assert manager._hydration_status == "ok"


def test_hydration_leaves_no_stub_when_the_checkout_disappears_after_cloning(
    tmp_path, monkeypatch
):
    # The clone validates, then the checkout is replaced before the exclude
    # write. _ensure_cli_dir plus an append-mode open would CREATE
    # .git/info/exclude in the empty replacement, and that stub is the artefact
    # every downstream isdir gate accepted.
    validated = {"done": False}

    def wipe_after_first_validation(path):
        if validated["done"]:
            return
        validated["done"] = True
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path)

    clones = []
    _install_fake_git(
        monkeypatch, clones=clones, on_validate=wipe_after_first_validation
    )
    manager = _hydration_manager(tmp_path, monkeypatch)

    manager._hydrate_workspace("jomcgi/homelab", "main")

    checkout = os.path.join(manager.workspace, "src")
    assert clones == [checkout]
    assert not os.path.exists(os.path.join(checkout, ".git"))
    assert manager._hydration_status is None
    assert manager._checkout_dir is None


def test_hydration_stops_re_cloning_at_the_attempt_cap(tmp_path, monkeypatch):
    # A checkout that keeps vanishing must not clone on every turn forever. The
    # cap is what turns the silent wrong answer into the loud StartupError the
    # drainer already recovers from.
    clones = []
    _install_fake_git(monkeypatch, clones=clones)
    manager = _hydration_manager(tmp_path, monkeypatch)

    for _ in range(shim.HYDRATION_ATTEMPT_CAP + 2):
        manager._hydrate_workspace("jomcgi/homelab", "main")
        shutil.rmtree(os.path.join(manager.workspace, "src"), ignore_errors=True)
        os.makedirs(os.path.join(manager.workspace, "src"))

    assert len(clones) == shim.HYDRATION_ATTEMPT_CAP
    assert manager._hydration_status is None


def test_turn_marks_every_adapter_as_repo_backed(tmp_path, monkeypatch):
    manager = _hydration_manager(tmp_path, monkeypatch)
    manager._mount_lock = threading.Lock()
    monkeypatch.setattr(shim, "ensure_workspace_volume", lambda: None)
    monkeypatch.setattr(shim, "_sync_session_volume", lambda: None)
    monkeypatch.setattr(shim, "apply_egress_ca_trust", lambda: None)
    monkeypatch.setattr(shim, "_ensure_cli_dir", lambda _path: None)
    monkeypatch.setattr(shim, "_capture_turn_base", lambda _dir: None)
    monkeypatch.setattr(
        shim.ProcessManager, "_hydrate_workspace", lambda self, repo, branch: None
    )
    monkeypatch.setattr(shim.ProcessManager, "_adapter", lambda self, model: self.pi)
    manager.pi.turn = lambda *args, **kwargs: {"text": "done"}

    manager.turn("hello", repo="jomcgi/homelab", branch="main")

    assert manager.claude.requires_git_checkout is True
    assert manager.codex.requires_git_checkout is True
    assert manager.pi.requires_git_checkout is True


def test_egress_copy_counts_bytes(capsys):
    payload = b"x" * (4 * 1024 * 1024 + 123)

    class FakeSource:
        def __init__(self, data):
            self.data = data
            self.done = False

        def recv(self, size):
            if self.done:
                return b""
            self.done = True
            return self.data

    class FakeDestination:
        def __init__(self):
            self.data = bytearray()
            self.shutdown_calls = []

        def sendall(self, data):
            self.data.extend(data)

        def shutdown(self, how):
            self.shutdown_calls.append(how)

    source = FakeSource(payload)
    destination = FakeDestination()
    shim.VsockEgressForwarder._copy(source, destination, "down")

    captured = capsys.readouterr().err
    assert len(destination.data) == len(payload)
    assert "egress-copy: down 1048576" in captured
    assert "egress-copy: down closed total=%s err=none" % len(payload) in captured
    assert destination.shutdown_calls == [socket.SHUT_WR]


def test_egress_copy_can_skip_half_close():
    class FakeSource:
        def __init__(self):
            self.done = False

        def recv(self, size):
            if self.done:
                return b""
            self.done = True
            return b"payload"

    class FakeDestination:
        def __init__(self):
            self.data = bytearray()
            self.shutdown_calls = []

        def sendall(self, data):
            self.data.extend(data)

        def shutdown(self, how):
            self.shutdown_calls.append(how)

    destination = FakeDestination()
    shim.VsockEgressForwarder._copy(
        FakeSource(), destination, "up", propagate_half_close=False
    )

    assert destination.data == b"payload"
    assert destination.shutdown_calls == []


def test_egress_forwarder_scopes_half_close_to_git_port(monkeypatch):
    # Port 9418 keeps the upstream write side open after the client
    # half-closes (the #4389 shape: git sends its request, half-closes, and
    # the bulk response must keep flowing); every other port propagates the
    # half-close so ordinary tunnels still tear down promptly.
    original_socket = socket.socket
    for port, expect_propagated in (("9418", False), ("443", True)):
        forwarder = shim.VsockEgressForwarder(port=0)
        vsock_peer, vsock_forwarder = socket.socketpair()

        class FakeVsock:
            def __init__(self):
                self.timeouts = []

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

            def connect(self, address):
                pass

            def recv(self, size):
                return vsock_forwarder.recv(size)

            def sendall(self, data):
                return vsock_forwarder.sendall(data)

            def shutdown(self, how):
                return vsock_forwarder.shutdown(how)

            def close(self):
                return vsock_forwarder.close()

        def socket_factory(family, *args, **kwargs):
            if family == shim.VSOCK_ADDRESS_FAMILY:
                return FakeVsock()
            return original_socket(family, *args, **kwargs)

        monkeypatch.setattr(shim.socket, "socket", socket_factory)
        forwarder.listen()
        client = socket.create_connection((shim.EGRESS_LOCALHOST, forwarder.port))
        try:
            client.sendall(("CONNECT example.com:%s HTTP/1.1\r\n\r\n" % port).encode())
            preamble = ("example.com:%s\n" % port).encode()
            assert vsock_peer.recv(len(preamble)) == preamble
            established = b"HTTP/1.1 200 Connection Established\r\n\r\n"
            assert client.recv(len(established)) == established
            client.sendall(b"request")
            assert vsock_peer.recv(7) == b"request"
            client.shutdown(socket.SHUT_WR)
            vsock_peer.settimeout(2)
            if expect_propagated:
                assert vsock_peer.recv(4096) == b""
            else:
                vsock_peer.sendall(b"late response")
                client.settimeout(2)
                assert client.recv(13) == b"late response"
        finally:
            client.close()
            vsock_peer.close()
            forwarder.close()


def test_git_proxy_helper_exits_when_response_goes_idle(tmp_path, monkeypatch):
    # The lane never propagates the client half-close onto vsock (#4389), so
    # the server-held connection stays open forever after the response ends.
    # git waits for the helper process to exit, so the helper must leave on
    # its own once the response has been idle past the deadline.
    helper_path = tmp_path / "ember-git-proxy"
    monkeypatch.setattr(shim, "GIT_PROXY_PATH", str(helper_path))
    shim._write_git_proxy_helper()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((shim.EGRESS_LOCALHOST, 0))
    listener.listen()
    accepted = []

    def serve_connection():
        connection, _ = listener.accept()
        accepted.append(connection)
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        connection.recv(4096)
        connection.sendall(b"response payload")
        # Deliberately neither close nor shutdown: the connection idles open,
        # exactly like the suppressed-half-close lane.
        time.sleep(8)

    accept_thread = threading.Thread(target=serve_connection, daemon=True)
    accept_thread.start()
    environment = os.environ.copy()
    environment[shim.EGRESS_PORT_ENV] = str(listener.getsockname()[1])
    environment["EMBER_GIT_PROXY_IDLE_EXIT_SECONDS"] = "1"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(helper_path), "example.com", "9418"],
            input=b"request",
            env=environment,
            capture_output=True,
            timeout=6,
        )
    finally:
        listener.close()
        for connection in accepted:
            connection.close()
    assert result.returncode == 0
    assert result.stdout == b"response payload"
    assert time.monotonic() - started < 6


def test_git_proxy_helper_default_idle_deadline_is_seconds_not_tens(
    tmp_path, monkeypatch
):
    # The DEFAULT is the one that runs in prod: the workload CR's initEnv is a
    # base-signature input that never reaches the guest environment, so there is
    # no chart override in play and this constant IS the hydration tail (#4429).
    # It regressed silently once (a 10s default charged to every clone, measured
    # at 10.9s wall for ~1.2s of transfer), so this asserts the default itself
    # with the env var deliberately unset rather than trusting the override path.
    helper_path = tmp_path / "ember-git-proxy"
    monkeypatch.setattr(shim, "GIT_PROXY_PATH", str(helper_path))
    shim._write_git_proxy_helper()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((shim.EGRESS_LOCALHOST, 0))
    listener.listen()
    accepted = []

    def serve_connection():
        connection, _ = listener.accept()
        accepted.append(connection)
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        connection.recv(4096)
        connection.sendall(b"response payload")
        time.sleep(9)

    accept_thread = threading.Thread(target=serve_connection, daemon=True)
    accept_thread.start()
    environment = os.environ.copy()
    environment[shim.EGRESS_PORT_ENV] = str(listener.getsockname()[1])
    environment.pop("EMBER_GIT_PROXY_IDLE_EXIT_SECONDS", None)
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(helper_path), "example.com", "9418"],
            input=b"request",
            env=environment,
            capture_output=True,
            timeout=8,
        )
    finally:
        listener.close()
        for connection in accepted:
            connection.close()
    assert result.returncode == 0
    assert result.stdout == b"response payload"
    # Comfortably under the old 10s default and over the 2s deadline plus the
    # helper's 0.5s poll granularity, so this fails loudly on a regression to
    # tens of seconds without being tight enough to flake on a slow runner.
    assert time.monotonic() - started < 6


def test_egress_forwarder_takes_absolute_uri_host_and_replays_the_request(monkeypatch):
    # HTTP_PROXY is set alongside HTTPS_PROXY, so a plain-HTTP request arrives as
    # an absolute-URI line rather than a CONNECT. The destination comes from Host,
    # and the whole head is replayed: an origin server must accept an absolute-URI
    # request line, so the forwarder never rewrites it.
    # The preamble and the replayed head are two separate writes, so read until
    # the second one lands rather than asserting on one recv (#4425).
    upstream_saw, _ = _forwarder_exchange(
        monkeypatch,
        b"GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\n\r\n",
        expect_upstream=b"GET http://example.com/x HTTP/1.1",
    )
    assert upstream_saw.startswith(b"example.com:80\n")
    assert b"GET http://example.com/x HTTP/1.1" in upstream_saw


def test_read_until_spans_separate_writes():
    # The bug #4425 recorded, isolated: two sendalls with a gap between them is
    # exactly what the forwarder does (preamble, then the replayed head), and a
    # single recv sees only the first. A read that stops at one recv fails here
    # deterministically, where against the live forwarder it only fails when the
    # scheduler happens to split the writes.
    reader, writer = socket.socketpair()

    def write_in_two_parts():
        writer.sendall(b"example.com:80\n")
        time.sleep(0.2)
        writer.sendall(b"GET http://example.com/x HTTP/1.1\r\n")

    thread = threading.Thread(target=write_in_two_parts, daemon=True)
    thread.start()
    try:
        saw = _read_until(reader, b"GET http://example.com/x HTTP/1.1")
    finally:
        thread.join(timeout=2)
        reader.close()
        writer.close()
    assert saw.startswith(b"example.com:80\n")
    assert b"GET http://example.com/x HTTP/1.1" in saw


def test_read_until_returns_what_arrived_when_nothing_is_expected():
    # expect=None is how a caller asserts the forwarder stayed silent, so this
    # must drain to the deadline and return empty rather than block forever.
    reader, writer = socket.socketpair()
    try:
        saw = _read_until(reader, None, deadline_seconds=0.2)
    finally:
        reader.close()
        writer.close()
    assert saw == b""


def test_egress_forwarder_rejects_a_head_it_cannot_route(monkeypatch):
    # No Host header and no CONNECT target: there is no destination to put in the
    # preamble, so the forwarder answers the client rather than opening a tunnel
    # to a guessed host.
    upstream_saw, client_saw = _forwarder_exchange(
        monkeypatch, b"GET /relative HTTP/1.1\r\n\r\n"
    )
    assert upstream_saw == b""
    assert client_saw.startswith(b"HTTP/1.1 400")


def test_session_id_mismatch_is_rejected_and_missing_id_resumes(tmp_path, monkeypatch):
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args_path))
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("first")
    manager.turn("matching", session_id="init-sid")
    with pytest.raises(shim.SessionConflictError, match="does not match"):
        manager.turn("wrong", session_id="other-sid")
    manager._close_process(kill=True)
    manager.turn("resume without id")
    args = json.loads(args_path.read_text())
    assert args[args.index("--resume") + 1] == "init-sid"
    manager._close_process(kill=True)


def _run_server(manager):
    server = ThreadingHTTPServer(("127.0.0.1", 0), shim.make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(server, method, path, body=None):
    connection = HTTPConnection("127.0.0.1", server.server_port)
    connection.request(method, path, body=body)
    response = connection.getresponse()
    return response.status, json.loads(response.read())


def test_http_health_ready_and_turn_gating(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    server = _run_server(manager)
    try:
        assert _request(server, "GET", shim.HEALTHZ_PATH)[0] == 200
        assert _request(server, "GET", shim.READY_PATH)[0] == 200
        status, body = _request(server, "POST", shim.TURN_PATH, b"{}")
        assert status == 400 and "empty" in body["error"]
        status, body = _request(
            server, "POST", shim.TURN_PATH, json.dumps({"message": "hi"}).encode()
        )
        assert status == 200 and body["session_id"] == "sid-1"
        assert _request(server, "GET", shim.READY_PATH)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        manager._close_process(kill=True)


def test_http_rejects_session_conflict_and_bad_content_length(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    server = _run_server(manager)
    try:
        status, _ = _request(
            server, "POST", shim.TURN_PATH, json.dumps({"message": "first"}).encode()
        )
        assert status == 200
        status, _ = _request(
            server,
            "POST",
            shim.TURN_PATH,
            json.dumps({"message": "resume", "session_id": None}).encode(),
        )
        assert status == 200
        status, body = _request(
            server,
            "POST",
            shim.TURN_PATH,
            json.dumps({"message": "wrong", "session_id": "other-sid"}).encode(),
        )
        assert status == 409 and "does not match" in body["error"]
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST", shim.TURN_PATH, body=b"{}", headers={"Content-Length": "bad"}
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        server.shutdown()
        server.server_close()
        manager._close_process(kill=True)


def test_http_clock_uses_noded_epoch_milliseconds(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    server = _run_server(manager)
    values = []
    monkeypatch.setattr(
        shim.time, "clock_settime", lambda clock, value: values.append((clock, value))
    )
    try:
        status, body = _request(server, "POST", shim.CLOCK_PATH, b"1760000000123")
        assert status == 200 and body["epoch_ms"] == 1760000000123
        assert values == [(shim.time.CLOCK_REALTIME, 1760000000.123)]
        assert _request(server, "POST", shim.CLOCK_PATH, b"invalid")[0] == 400
    finally:
        server.shutdown()
        server.server_close()


def test_cli_crash_is_422(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    original = manager._spawn

    def crash_spawn(session_id=None, first_message=None, model=None, **_kwargs):
        raise RuntimeError("claude crashed during turn, exit code 7")

    manager._spawn = crash_spawn
    server = _run_server(manager)
    try:
        status, body = _request(
            server, "POST", shim.TURN_PATH, json.dumps({"message": "hi"}).encode()
        )
        assert status == 422 and "crashed" in body["error"]
    finally:
        manager._spawn = original
        server.shutdown()
        server.server_close()


def test_unparseable_line_logged_to_stderr_and_retained(tmp_path, monkeypatch, capsys):
    """Verify unparseable lines go to stderr and are retained in the ring."""
    manager = _manager(tmp_path, monkeypatch)
    manager.turn("make changes")
    captured = capsys.readouterr()
    assert "ember-claude-shim: cli-stdout: not json" in captured.err
    assert "not json" in manager.unparseable_lines
    manager._close_process(kill=True)


def test_init_failure_includes_ring_buffer_in_error_message(tmp_path, monkeypatch):
    """Verify init-failure errors include unparseable CLI output."""
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'print("unparseable line 1", flush=True)\n'
        'print("unparseable line 2", flush=True)\n'
        "sys.exit(1)\n"
    )
    os.chmod(executable, 0o755)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    with pytest.raises(RuntimeError) as exc_info:
        manager.turn("hello")

    error_msg = str(exc_info.value)
    assert "claude exited before init" in error_msg
    assert "unparseable line" in error_msg


def _write_delayed_init_cli(tmp_path, delay):
    executable = tmp_path / "delayed-init-cli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    sys.exit(0)\n"
        "line = sys.stdin.readline()\n"
        "print('init output before timeout', flush=True)\n"
        "print('init stderr before timeout', file=sys.stderr, flush=True)\n"
        "print(json.dumps({'type': 'error', 'message': 'init still starting'}), flush=True)\n"
        "time.sleep(%s)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's',\n"
        "                  'apiKeySource': 'none', 'mcp_servers': []}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'terminal_reason': 'end_turn',\n"
        "                  'stop_reason': 'end_turn', 'is_error': False,\n"
        "                  'permission_denials': [], 'num_turns': 1, 'session_id': 's',\n"
        "                  'usage': {}, 'total_cost_usd': 0, 'modelUsage': {},\n"
        "                  'duration_ms': 1}), flush=True)\n" % delay
    )
    os.chmod(executable, 0o755)
    return executable


def test_init_timeout_includes_diagnostics_and_honors_env_override(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("EMBER_INIT_READ_TIMEOUT", "0.2")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _write_delayed_init_cli(tmp_path, 1)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    with pytest.raises(shim.StartupError) as exc_info:
        manager.turn("test")

    error_msg = str(exc_info.value)
    assert "after 0.2 seconds" in error_msg
    assert "CLI output:" in error_msg
    assert "CLI stderr:" in error_msg
    assert "Parsed events:" in error_msg


def test_init_timeout_env_override_honored(tmp_path, monkeypatch):
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("EMBER_INIT_READ_TIMEOUT", "5.0")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _write_delayed_init_cli(tmp_path, 6)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    with pytest.raises(shim.StartupError, match="after 5.0 seconds"):
        manager.turn("test")


def test_init_timeout_env_override_falls_back_to_default_on_garbage(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("EMBER_INIT_READ_TIMEOUT", "not a number")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _write_delayed_init_cli(tmp_path, 15)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    assert manager.turn("test")["terminal_reason"] == "end_turn"
    manager._close_process(kill=True)


def test_unparseable_line_truncation(tmp_path, monkeypatch, capsys):
    """Verify CLI lines are capped at 2000 chars and errors at about 1500."""
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'print("x" * 3000, flush=True)\n'
        "sys.exit(1)\n"
    )
    os.chmod(executable, 0o755)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    with pytest.raises(RuntimeError) as exc_info:
        manager.turn("test")
    capsys.readouterr()

    error_msg = str(exc_info.value)
    assert len(manager.unparseable_lines[-1]) == 2000
    assert len(error_msg) < 3500
    assert "truncated" in error_msg


def test_cli_startup_probe_logged(tmp_path, monkeypatch, capsys):
    """Verify CLI --version probe is logged at startup."""
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    manager = _manager(tmp_path, monkeypatch)
    captured = capsys.readouterr()
    # The probe should log something with "cli-probe:" prefix
    assert "ember-claude-shim: cli-probe:" in captured.err
    manager._close_process(kill=True)


def test_stderr_lines_captured_to_ring_and_console(tmp_path, monkeypatch, capsys):
    """Stderr lines land in the ring with a stderr prefix and on the console."""
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    fake_cli_code = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "line = sys.stdin.readline()\n"
        "sys.stderr.write('stderr line 1\\n')\n"
        "sys.stderr.write('stderr line 2\\n')\n"
        "sys.stderr.flush()\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's',\n"
        "                  'apiKeySource': 'none', 'mcp_servers': []}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'terminal_reason': 'end_turn',\n"
        "                  'stop_reason': 'end_turn', 'is_error': False, 'permission_denials': [],\n"
        "                  'num_turns': 1, 'session_id': 's', 'usage': {}, 'total_cost_usd': 0,\n"
        "                  'modelUsage': {}, 'duration_ms': 1}), flush=True)\n"
    )
    executable.write_text(fake_cli_code)
    os.chmod(executable, 0o755)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    manager.turn("test")
    manager._close_process(kill=True)
    # The pump drains asynchronously after process exit: each line hits the
    # console before the ring, so once the ring holds both lines the console
    # writes have happened too.
    deadline = time.time() + 5
    while (
        time.time() < deadline
        and sum(1 for line in manager.stderr_lines if "stderr line" in line) < 2
    ):
        time.sleep(0.05)
    captured = capsys.readouterr()

    assert "ember-claude-shim: cli-stderr: stderr line 1" in captured.err
    assert "ember-claude-shim: cli-stderr: stderr line 2" in captured.err
    assert any("stderr line" in line for line in manager.stderr_lines)


def test_parsed_non_init_events_retained(tmp_path, monkeypatch):
    """Parseable non-init events emitted before init are retained in the ring."""
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    fake_cli_code = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "line = sys.stdin.readline()\n"
        "print(json.dumps({'type': 'error', 'message': 'pre-init error'}), flush=True)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's',\n"
        "                  'apiKeySource': 'none', 'mcp_servers': []}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'terminal_reason': 'end_turn',\n"
        "                  'stop_reason': 'end_turn', 'is_error': False, 'permission_denials': [],\n"
        "                  'num_turns': 1, 'session_id': 's', 'usage': {}, 'total_cost_usd': 0,\n"
        "                  'modelUsage': {}, 'duration_ms': 1}), flush=True)\n"
    )
    executable.write_text(fake_cli_code)
    os.chmod(executable, 0o755)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    manager.turn("test")

    assert len(manager.parsed_events) > 0
    assert any("pre-init error" in event for event in manager.parsed_events)
    manager._close_process(kill=True)


def test_parsed_event_in_init_failure_message(tmp_path, monkeypatch):
    """Verify parsed events appear in init-failure error messages."""
    monkeypatch.setattr(shim.os, "geteuid", lambda: 1000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "fake-cli"
    fake_cli_code = r"""#!/usr/bin/env python3
import sys
# Emit an error event then exit
print('{"type": "error", "message": "startup failed"}', flush=True)
sys.exit(1)
"""
    executable.write_text(fake_cli_code)
    os.chmod(executable, 0o755)
    monkeypatch.setenv("EMBER_GIT_USER_NAME", "Test User")
    monkeypatch.setenv("EMBER_GIT_USER_EMAIL", "test@example.invalid")
    manager = shim.ClaudeProcess(str(workspace), str(executable))
    monkeypatch.setattr(manager, "_configure_git", lambda: None)

    with pytest.raises(RuntimeError) as exc_info:
        manager.turn("test")

    error_msg = str(exc_info.value)
    assert "claude exited before init" in error_msg
    assert "Parsed events:" in error_msg or "error" in error_msg


def test_pi_argv_constrains_the_context_budget(tmp_path, monkeypatch):
    """pi must be launched with the context budget pinned down.

    Pi gets one bounded share of NInfer's shared KV pool. These flags keep that
    budget available to the task. Losing any of them fails SILENTLY: the turn
    still succeeds, it just spends the window on discovered context or a default
    prompt instead of the task, and the answers quietly get worse.
    """
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen", system_prompt="CALLER-MARKER")
    argv = json.loads((tmp_path / "pi-args.jsonl").read_text().splitlines()[0])

    # --system-prompt REPLACES pi's default coding prompt (it does not append),
    # which is what keeps the scaffolding down to a couple of hundred tokens.
    assert "--system-prompt" in argv
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert "You are a focused coding agent." in system_prompt
    assert shim.SANDBOX_PROMPT in system_prompt
    assert "CALLER-MARKER" in system_prompt

    # Discovery of every kind is off: context files (AGENTS.md/CLAUDE.md),
    # extensions, skills and prompt templates all inject tokens we did not
    # choose, and AGENTS.md discovery alone can dwarf the prompt.
    for flag in (
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
    ):
        assert flag in argv, "missing %s: Qwen's context budget is unguarded" % flag

    # Explicitly load the trusted extension while all discovery stays off.
    assert "--extension" in argv
    assert argv[argv.index("--extension") + 1] == shim.PI_WEB_RESEARCH_EXTENSION
    manager._close_process()


def test_pi_argv_does_not_allowlist_away_extension_tools(tmp_path, monkeypatch):
    """Pi's built-in allowlist must not block the trusted web extension."""
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen")
    argv = json.loads((tmp_path / "pi-args.jsonl").read_text().splitlines()[0])

    assert argv[argv.index("--extension") + 1] == shim.PI_WEB_RESEARCH_EXTENSION
    # Pi treats --tools as a COMPLETE allowlist. The old read,bash,edit,write
    # value silently prevented web_search and web_fetch from becoming active at
    # session_start. With no allowlist, Pi keeps those same four default
    # built-ins and permits the explicitly loaded extension tools to join them.
    assert "--tools" not in argv
    manager._close_process()


def test_pi_models_json_declares_correct_context_window(tmp_path, monkeypatch):
    """models.json must declare the bounded per-session NInfer budget.

    If these values are wrong, pi's clampMaxTokensToContext computes available
    tokens using a false budget. Pi's 128000-token default would let two active
    sessions consume 256000 tokens, leaving less than the required shared
    headroom in NInfer's 262144-token page pool.
    """
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen")

    assert (
        shim.PI_CONTEXT_WINDOW
        - shim.PI_MAX_OUTPUT_TOKENS
        - shim.PI_CONTEXT_SAFETY_TOKENS
        > 0
    ), "Context window too small: max output tokens plus safety margin exceeds budget"
    assert shim.PI_CONTEXT_WINDOW == 122880

    models_path = tmp_path / "workspace" / ".pi" / "agent" / "models.json"
    models = json.loads(models_path.read_text())
    provider = models["providers"]["openai-completions"]
    model_dict = provider["models"][0]

    assert provider["compat"]["thinkingFormat"] == "qwen-chat-template"
    assert model_dict["contextWindow"] == shim.PI_CONTEXT_WINDOW
    assert model_dict["maxTokens"] == shim.PI_MAX_OUTPUT_TOKENS
    assert model_dict["reasoning"] is True

    manager._close_process()


def test_pi_context_window_headroom_relationship():
    """Two Pi windows plus shared headroom must fit NInfer's KV capacity.

    The cross-file test reads the deployment values. This fast unit test pins
    the approved production arithmetic even without its Bazel data dependency.
    """
    ninfer_kv_capacity = 262144
    ninfer_max_concurrency = 2
    declared_window = shim.PI_CONTEXT_WINDOW
    declared_headroom = shim.PI_CONTEXT_WINDOW_HEADROOM
    assert declared_window == 122880
    assert declared_headroom == 16384

    total = declared_window * ninfer_max_concurrency + declared_headroom
    assert total == ninfer_kv_capacity, (
        "%s Pi contexts at %s tokens plus %s headroom = %s, which does not "
        "exactly account for NInfer KV capacity %s."
        % (
            ninfer_max_concurrency,
            declared_window,
            declared_headroom,
            total,
            ninfer_kv_capacity,
        )
    )


def test_pi_settings_json_compaction_reserve_exceeds_max_output(tmp_path, monkeypatch):
    """Compaction reserve must exceed max output plus pi's safety margin."""
    assert (
        shim.PI_COMPACTION_RESERVE_TOKENS
        > shim.PI_MAX_OUTPUT_TOKENS + shim.PI_CONTEXT_SAFETY_TOKENS
    ), "Compaction reserve too small: full response may not fit when compaction fires"


def test_pi_default_thinking_level_is_valid():
    """The configured default must be one of pi's ThinkingLevel strings."""
    assert shim.PI_DEFAULT_THINKING_LEVEL in {
        "off",
        "minimal",
        "low",
        "medium",
        "high",
    }


def test_pi_settings_json_keep_recent_smaller_than_usable_window(tmp_path, monkeypatch):
    """keepRecentTokens must be less than the usable window after compaction."""
    usable_after_reserve = shim.PI_CONTEXT_WINDOW - shim.PI_COMPACTION_RESERVE_TOKENS
    assert shim.PI_COMPACTION_KEEP_RECENT_TOKENS < usable_after_reserve, (
        "keepRecentTokens exceeds usable window: compaction will not reduce context"
    )


def test_pi_settings_json_is_written_with_compaction_block(tmp_path, monkeypatch):
    """settings.json must contain pi's compaction configuration."""
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen")

    settings_path = tmp_path / "workspace" / ".pi" / "agent" / "settings.json"
    settings = json.loads(settings_path.read_text())

    assert "compaction" in settings
    assert settings["compaction"]["enabled"] is True
    assert settings["compaction"]["reserveTokens"] == shim.PI_COMPACTION_RESERVE_TOKENS
    assert (
        settings["compaction"]["keepRecentTokens"]
        == shim.PI_COMPACTION_KEEP_RECENT_TOKENS
    )
    assert settings["defaultThinkingLevel"] == shim.PI_DEFAULT_THINKING_LEVEL

    manager._close_process()


def test_pi_settings_json_merge_preserves_unrelated_keys(tmp_path, monkeypatch):
    """Existing settings.json unrelated keys must survive spawn."""
    manager = _pi_manager(tmp_path, monkeypatch)

    settings_dir = tmp_path / "workspace" / ".pi" / "agent"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(json.dumps({"custom": {"key": "value"}}))

    manager.turn("hello", model="qwen")

    settings = json.loads(settings_path.read_text())
    assert settings["custom"] == {"key": "value"}, (
        "Merge did not preserve unrelated key"
    )
    assert "compaction" in settings, "Merge did not add compaction block"

    manager._close_process()


def test_pi_settings_json_corrupt_file_does_not_break_spawn(tmp_path, monkeypatch):
    """Corrupt settings.json must not break spawn."""
    manager = _pi_manager(tmp_path, monkeypatch)

    settings_dir = tmp_path / "workspace" / ".pi" / "agent"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text("{invalid json content")

    manager.turn("hello", model="qwen")

    settings = json.loads(settings_path.read_text())
    assert "compaction" in settings, "Corrupt file was not replaced with fresh content"
    assert settings["defaultThinkingLevel"] == shim.PI_DEFAULT_THINKING_LEVEL

    manager._close_process()


def test_pi_settings_json_invalid_utf8_does_not_crash(tmp_path, monkeypatch):
    """Invalid UTF-8 in settings.json must not crash _write_settings_json.

    A torn write on the durable workspace volume can produce invalid UTF-8 bytes.
    UnicodeDecodeError must be caught and handled gracefully without crashing the
    spawn.
    """
    manager = _pi_manager(tmp_path, monkeypatch)

    settings_dir = tmp_path / "workspace" / ".pi" / "agent"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    # Write genuinely invalid UTF-8 bytes
    settings_path.write_bytes(b"\xff\xfe\x00garbage")

    # This should not crash; the spawn should proceed
    manager.turn("hello", model="qwen")

    settings = json.loads(settings_path.read_text())
    assert "compaction" in settings, (
        "Invalid UTF-8 should be replaced with fresh content"
    )

    manager._close_process()


def test_pi_settings_json_valid_non_object_json_does_not_crash(tmp_path, monkeypatch):
    """Valid JSON that is not an object must not crash _write_settings_json.

    json.load succeeds on [], "hello", null, and 3. The method must guard
    against these and fall back to a fresh dict without crashing the spawn.
    """
    manager = _pi_manager(tmp_path, monkeypatch)

    settings_dir = tmp_path / "workspace" / ".pi" / "agent"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(json.dumps([1, 2, 3]))

    manager.turn("hello", model="qwen")

    settings = json.loads(settings_path.read_text())
    assert isinstance(settings, dict), "Non-dict JSON should be replaced with dict"
    assert "compaction" in settings

    manager._close_process()


def test_pi_settings_json_existing_compaction_block_replaced(tmp_path, monkeypatch):
    """A pre-existing compaction block must be replaced wholesale, not merged."""
    manager = _pi_manager(tmp_path, monkeypatch)

    settings_dir = tmp_path / "workspace" / ".pi" / "agent"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "compaction": {"enabled": False, "reserveTokens": 1000},
                "custom": {"key": "value"},
            }
        )
    )

    manager.turn("hello", model="qwen")

    settings = json.loads(settings_path.read_text())
    assert settings["compaction"]["enabled"] is True
    assert settings["compaction"]["reserveTokens"] == shim.PI_COMPACTION_RESERVE_TOKENS
    assert settings["custom"] == {"key": "value"}

    manager._close_process()


def test_codex_auth_json_is_parseable_and_inert(tmp_path, monkeypatch):
    """The CLI parses these tokens, so shape matters more than content."""
    import base64 as _b64

    manager = _codex_manager(tmp_path, monkeypatch)
    record = manager.turn("hello", model="luna")
    assert record["usage"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert record["activities"] == [{"type": "bash", "command": "echo test"}]
    auth_path = os.path.join(str(tmp_path / "workspace"), ".codex", "auth.json")
    auth = json.loads(open(auth_path).read())
    assert auth["auth_mode"] == "chatgpt"
    assert auth["OPENAI_API_KEY"] is None
    token = auth["tokens"]["access_token"]
    parts = token.split(".")
    assert len(parts) == 3, "tokens must be JWT-shaped or the CLI reads as logged out"
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(_b64.urlsafe_b64decode(padded))
    assert claims["exp"] > 4_000_000_000, (
        "expiry must be far future so the guest never self-refreshes"
    )
    manager._close_process()


def test_install_egress_ca_appends_to_system_bundle(tmp_path, monkeypatch):
    """The fetched CA is APPENDED to the image's trust store, never replaces it.

    The guest still has to verify the real public internet on every host the
    sidecar merely tunnels, so replacing the bundle would trade one MITM lane for
    a guest that trusts nothing else.
    """
    system = tmp_path / "system.crt"
    system.write_bytes(
        b"-----BEGIN CERTIFICATE-----\nSYSTEM\n-----END CERTIFICATE-----\n"
    )
    out = tmp_path / "bundle.crt"
    monkeypatch.setattr(shim, "SYSTEM_CA_BUNDLE", str(system))
    monkeypatch.setattr(shim, "CA_BUNDLE_PATH", str(out))
    monkeypatch.setattr(
        shim,
        "fetch_egress_ca",
        lambda *a, **k: (
            b"-----BEGIN CERTIFICATE-----\nEGRESS\n-----END CERTIFICATE-----\n"
        ),
    )

    assert shim.install_egress_ca() == str(out)
    written = out.read_bytes()
    assert b"SYSTEM" in written
    assert b"EGRESS" in written


def test_install_egress_ca_returns_none_without_a_ca(tmp_path, monkeypatch):
    """No CA served means leave every trust variable unset.

    A guest that trusted a CA it failed to fetch would fail every TLS handshake;
    staying on the stock trust store only loses credential injection.
    """
    monkeypatch.setattr(shim, "CA_BUNDLE_PATH", str(tmp_path / "bundle.crt"))
    monkeypatch.setattr(shim, "fetch_egress_ca", lambda *a, **k: None)
    assert shim.install_egress_ca() is None


def test_fetch_egress_ca_rejects_a_non_pem_response(monkeypatch):
    """A sidecar with no CA closes without writing; anything that is not a
    certificate must read as absent rather than be written into the bundle."""

    class _Sock:
        def settimeout(self, _):
            pass

        def connect(self, _):
            pass

        def sendall(self, _):
            pass

        def recv(self, _):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(shim.socket, "socket", lambda *a, **k: _Sock())
    monkeypatch.setattr(shim, "VSOCK_ADDRESS_FAMILY", 40)
    assert shim.fetch_egress_ca() is None


def test_apply_egress_ca_trust_exports_every_tool_variable(tmp_path, monkeypatch):
    """One bundle, five variables, because each tool reads a different one.

    gh is Go (SSL_CERT_FILE), git is curl (GIT_SSL_CAINFO), the CLI is bun
    (NODE_EXTRA_CA_CERTS). Missing any one leaves that tool unable to verify the
    sidecar's minted leaf, which surfaces as "self-signed certificate in
    certificate chain" rather than as a missing credential.
    """
    bundle = tmp_path / "bundle.crt"
    monkeypatch.setattr(shim, "install_egress_ca", lambda: str(bundle))
    for key in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
    ):
        monkeypatch.delenv(key, raising=False)

    assert shim.apply_egress_ca_trust() == str(bundle)
    for key in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert os.environ[key] == str(bundle), key


def test_apply_egress_ca_trust_sets_nothing_without_a_ca(monkeypatch):
    """No CA means leave the stock trust store alone, not point at a missing file."""
    monkeypatch.setattr(shim, "install_egress_ca", lambda: None)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert shim.apply_egress_ca_trust() is None
    assert "SSL_CERT_FILE" not in os.environ


def test_turn_base_scopes_safe_directory_to_the_checkout(monkeypatch, tmp_path):
    # The shim is root and hydration clones as the CLI uid, so git sees a
    # repository owned by someone else and refuses. safe.directory is passed per
    # invocation and scoped to this checkout, never written to a git config.
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, b"a" * 40 + b"\n", b"")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    assert shim._capture_turn_base(str(checkout)) == "a" * 40
    assert seen["args"][:3] == [
        "git",
        "-c",
        "safe.directory=%s" % checkout,
    ]
    assert "rev-parse" in seen["args"]


def test_turn_diff_scopes_safe_directory_to_the_checkout(monkeypatch, tmp_path):
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, b"diff --git a/a b/a\n", b"")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)

    assert shim._capture_turn_diff(str(checkout), "b" * 40) is not None
    # Every git read (the diff and the untracked-file listing) is scoped.
    for args in seen:
        assert args[:3] == ["git", "-c", "safe.directory=%s" % checkout]
    assert any("diff" in args for args in seen)


def test_turn_base_failure_reason_includes_git_stderr(monkeypatch, tmp_path, capsys):
    # git already writes the reason. Discarding it via DEVNULL is what made
    # rev_parse_failed unactionable for hours: the outcome named which guard
    # fired, never why.
    checkout = tmp_path / "src"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        shim.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 128, b"", b"fatal: detected dubious ownership in repository"
        ),
    )

    assert shim._capture_turn_base(str(checkout)) is None
    captured = capsys.readouterr().err
    assert "outcome=rev_parse_failed" in captured
    assert "dubious ownership" in captured
