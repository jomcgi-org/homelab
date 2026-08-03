"""Unit tests for the Claude guest shim using a fake stream-json CLI."""

import json
import ast
import datetime
import os
import signal
import socket
import threading
import time
import subprocess
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import shim


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
            {key: os.environ.get(key) for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")},
            stream,
        )
api_key = os.environ.get("FAKE_API_KEY", "none")
print(json.dumps({"type": "system", "subtype": "init", "session_id": "init-sid",
                  "model": "fake", "apiKeySource": api_key, "mcp_servers": []}), flush=True)

def interrupted(_signum, _frame):
    sys.exit(0)

signal.signal(signal.SIGINT, interrupted)
for line in sys.stdin:
    request = json.loads(line)
    text = request["message"]["content"][0]["text"]
    if text == "block":
        while True:
            signal.pause()
    print("not json", flush=True)
    if text == "fallback":
        result = "First sentence. Second sentence."
    else:
        result = "Done <voice>Changed the files and need review.</voice>"
    permission_denials = []
    terminal_reason = "end_turn"
    if text == "permission denied":
        permission_denials = [{
            "tool_name": "Write",
            "tool_use_id": "toolu_test",
            "reason": "requires_approval"
        }]
        terminal_reason = "completed"
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.txt"}},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "b.txt"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "c.txt"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}
    ]}}), flush=True)
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
assert "--skip-git-repo-check" in sys.argv
assert "--model" in sys.argv
assert "--config" in sys.argv
model = sys.argv[sys.argv.index("--model") + 1]
effort = sys.argv[sys.argv.index("--config") + 1]
assert model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")
assert effort in ("model_reasoning_effort=medium", "model_reasoning_effort=high")
print(json.dumps({"type": "thread.started", "thread_id": "codex-thread"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {
    "type": "agent_message",
    "text": "Done <voice>Codex completed the work.</voice>"
}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 3, "output_tokens": 4
}}), flush=True)
if os.environ.get("FAKE_CODEX_SLEEP"):
    time.sleep(float(os.environ["FAKE_CODEX_SLEEP"]))
"""


FAKE_PI_CLI = r"""#!/usr/bin/env python3
import json
import os
import sys

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
assert sys.argv[sys.argv.index("--tools") + 1] == "read,bash,edit,write"
assert "End every response with a single line" in sys.argv[sys.argv.index("--system-prompt") + 1]
if os.environ.get("FAKE_PI_MODE") == "provider-error":
    print(json.dumps({"type": "session", "id": "pi-session", "version": 3}), flush=True)
    print(json.dumps({"type": "agent_end", "messages": [{
        "role": "assistant",
        "content": [],
        "errorMessage": "connect ECONNREFUSED inference.inference.svc.cluster.local:8080"
    }]}), flush=True)
    sys.exit(0)
print(json.dumps({"type": "session", "id": "pi-session", "version": 3}), flush=True)
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant",
    "content": [{"type": "text", "text": "Done <voice>Pi completed the work.</voice>"}],
    "stopReason": "stop",
    "usage": {"input": 5, "output": 7}
}}), flush=True)
print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
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


def test_pi_resume_uses_session_flag(tmp_path, monkeypatch):
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("first", model="qwen")
    manager.turn("second", session_id="pi-session", model="qwen")
    args = [
        json.loads(line)
        for line in (tmp_path / "pi-args.jsonl").read_text().splitlines()
    ]
    assert "--session" in args[1] and "pi-session" in args[1]
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


def test_codex_first_turn_returns_thread_voice_and_usage(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    record = manager.turn("first", model="luna")
    assert record == {
        "result": "Done <voice>Codex completed the work.</voice>",
        "terminal_reason": "completed",
        "session_id": "codex-thread",
        "usage": {"input_tokens": 3, "output_tokens": 4},
        "voice": "Codex completed the work.",
        "activity": [],
    }
    manager._close_process()


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
    config = (tmp_path / "workspace" / ".codex" / "config.toml").read_text()
    assert 'base_url = "http://broker.test/backend-api/codex/"' in config
    assert 'chatgpt_base_url = "%s"' % endpoint in config
    assert "api.openai.com" not in config
    assert 'name = "ember-openai"' in config
    assert 'wire_api = "responses"' in config
    assert "enable_codex_api_key_env = false" in config


def test_codex_child_env_drops_openai_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-codex")
    manager = _codex_manager(tmp_path, monkeypatch)
    assert "OPENAI_API_KEY" not in manager._child_env()


def test_codex_resume_uses_positional_session_and_no_sandbox(tmp_path, monkeypatch):
    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("first", model="terra")
    manager.turn("second", session_id="codex-thread", model="terra")
    calls = [
        json.loads(line)
        for line in (tmp_path / "codex-args.jsonl").read_text().splitlines()
    ]
    resume = calls[1]
    assert resume[:4] == ["exec", "resume", "codex-thread", "second"]
    assert "--sandbox" not in resume
    assert resume[resume.index("--model") + 1] == "gpt-5.6-terra"
    assert resume[resume.index("--config") + 1] == "model_reasoning_effort=high"
    manager._close_process()


def test_codex_returns_at_turn_completed_without_waiting_for_exit(
    tmp_path, monkeypatch
):
    manager = _codex_manager(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    started = time.monotonic()
    manager.turn("fast", model="sol")
    elapsed = time.monotonic() - started
    assert elapsed < 0.1
    assert manager.process.poll() is None
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


def test_turn_extracts_voice_activity_and_tolerates_malformed_json(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    record = manager.turn("make changes")
    assert record["terminal_reason"] == "end_turn"
    assert record["voice"] == "Changed the files and need review."
    assert record["activity"] == [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.txt"}},
        {"type": "edit", "file_path": "b.txt"},
        {"type": "write", "file_path": "c.txt"},
        {"type": "bash", "command": "git status"},
    ]
    assert manager.ready()
    manager._close_process(kill=True)


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
    manager = object.__new__(shim.ProcessManager)

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
    }
    manager._close_process(kill=True)


def test_egress_forwarder_opens_one_vsock_connection_per_accept(monkeypatch):
    forwarder = shim.VsockEgressForwarder(port=0)
    original_socket = socket.socket
    vsock_peer, vsock_forwarder = socket.socketpair()
    connected = []

    class FakeVsock:
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
            return FakeVsock()
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
    finally:
        client.close()
        vsock_peer.close()
        forwarder.close()


def _forwarder_exchange(monkeypatch, request_bytes):
    """Drive one connection through the forwarder, returning what the lane saw."""
    forwarder = shim.VsockEgressForwarder(port=0)
    original_socket = socket.socket
    vsock_peer, vsock_forwarder = socket.socketpair()

    class FakeVsock:
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
        vsock_peer.settimeout(2)
        client.settimeout(2)
        try:
            upstream_saw = vsock_peer.recv(4096)
        except (TimeoutError, OSError):
            upstream_saw = b""
        try:
            client_saw = client.recv(4096)
        except (TimeoutError, OSError):
            client_saw = b""
        return upstream_saw, client_saw
    finally:
        client.close()
        vsock_peer.close()
        forwarder.close()


def test_egress_forwarder_takes_absolute_uri_host_and_replays_the_request(monkeypatch):
    # HTTP_PROXY is set alongside HTTPS_PROXY, so a plain-HTTP request arrives as
    # an absolute-URI line rather than a CONNECT. The destination comes from Host,
    # and the whole head is replayed: an origin server must accept an absolute-URI
    # request line, so the forwarder never rewrites it.
    upstream_saw, _ = _forwarder_exchange(
        monkeypatch,
        b"GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\n\r\n",
    )
    assert upstream_saw.startswith(b"example.com:80\n")
    assert b"GET http://example.com/x HTTP/1.1" in upstream_saw


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

    def crash_spawn(session_id=None, first_message=None, model=None):
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
        "print('init output before timeout', flush=True)\n"
        "print('init stderr before timeout', file=sys.stderr, flush=True)\n"
        "print(json.dumps({'type': 'error', 'message': 'init still starting'}), flush=True)\n"
        "time.sleep(%s)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's',\n"
        "                  'apiKeySource': 'none', 'mcp_servers': []}), flush=True)\n"
        "sys.stdin.readline()\n"
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
        "sys.stderr.write('stderr line 1\\n')\n"
        "sys.stderr.write('stderr line 2\\n')\n"
        "sys.stderr.flush()\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's',\n"
        "                  'apiKeySource': 'none', 'mcp_servers': []}), flush=True)\n"
        "line = sys.stdin.readline()\n"
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
        "print(json.dumps({'type': 'error', 'message': 'pre-init error'}), flush=True)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's',\n"
        "                  'apiKeySource': 'none', 'mcp_servers': []}), flush=True)\n"
        "line = sys.stdin.readline()\n"
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

    Qwen serves a 32768-token window. These flags are the entire reason pi was
    chosen over the claude CLI, and losing any of them fails SILENTLY: the turn
    still succeeds, it just spends the window on discovered context or a default
    prompt instead of the task, and the answers quietly get worse.
    """
    manager = _pi_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="qwen")
    argv = json.loads((tmp_path / "pi-args.jsonl").read_text().splitlines()[0])

    # --system-prompt REPLACES pi's default coding prompt (it does not append),
    # which is what keeps the scaffolding down to a couple of hundred tokens.
    assert "--system-prompt" in argv
    assert shim.VOICE_PROMPT in argv[argv.index("--system-prompt") + 1]

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

    # A small explicit tool set: every tool schema is tokens, and a 27B model
    # handles four tools better than the claude CLI's twenty six.
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == "read,bash,edit,write"
    manager._close_process()


def test_codex_auth_json_is_parseable_and_inert(tmp_path, monkeypatch):
    """The CLI parses these tokens, so shape matters more than content."""
    import base64 as _b64

    manager = _codex_manager(tmp_path, monkeypatch)
    manager.turn("hello", model="luna")
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
