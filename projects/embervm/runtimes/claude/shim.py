#!/usr/bin/python3
"""HTTP over vsock shim for a long-lived Claude Code CLI session."""

import http.server
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time


GUEST_HTTP_PORT = 1027
HEALTHZ_PATH = "/shim/healthz"
READY_PATH = "/shim/ready"
TURN_PATH = "/shim/turn"
INTERRUPT_PATH = "/shim/interrupt"
CLOCK_PATH = "/shim/clock"
DEFAULT_WORKSPACE = "/workspace"
MAX_REQUEST_BODY_BYTES = 1 << 20
MAX_TOOL_INPUT_BYTES = 4096
INIT_READ_TIMEOUT = 15.0
TURN_READ_TIMEOUT = 60.0
INTERRUPT_TIMEOUT = 30.0
PERMISSION_MODE_ENV = "EMBER_PERMISSION_MODE"
DEFAULT_PERMISSION_MODE = "bypassPermissions"
VOICE_PROMPT = (
    "End every response with a single line: <voice>One or two plain sentences, "
    "no markdown, that a person could hear read aloud: what you did and anything "
    "you need from them.</voice>"
)


class StartupError(Exception):
    pass


class SessionConflictError(StartupError):
    pass


_managed_child_pids = set()


def _reap_orphans(_signum=None, _frame=None):
    """Reap adopted grandchildren while leaving Popen-managed CLIs alone."""
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, flags)
        except (ChildProcessError, OSError):
            return
        if info is None or info.si_pid == 0:
            return
        if info.si_pid in _managed_child_pids:
            # Popen must reap its direct child itself to preserve its exit status.
            return
        try:
            os.waitpid(info.si_pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


def install_child_reaper():
    # PID 1 adopts grandchildren left by background Bash tools. Reap only those
    # children, because reaping the CLI here would break subprocess wait/poll.
    signal.signal(signal.SIGCHLD, _reap_orphans)


def _json_line(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


def voice_summary(result):
    text = result if isinstance(result, str) else str(result or "")
    match = re.search(r"<voice>\s*(.*?)\s*</voice>", text, re.DOTALL)
    if match and match.group(1).strip():
        return re.sub(r"\s+", " ", match.group(1).strip())[:200]
    sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    return sentence[:200]


def _input_value(value, key):
    return value.get(key, "") if isinstance(value, dict) else value


def _bounded_tool_input(value):
    try:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return "[omitted: non-JSON tool input]"
    if len(encoded) > MAX_TOOL_INPUT_BYTES:
        return "[omitted: tool input exceeds %d bytes]" % MAX_TOOL_INPUT_BYTES
    return value


def activity_from_events(events):
    activity = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            value = block.get("input")
            if block_type == "tool_use":
                name = block.get("name")
                if name == "Edit":
                    activity.append(
                        {"type": "edit", "file_path": _input_value(value, "file_path")}
                    )
                elif name == "Write":
                    activity.append(
                        {"type": "write", "file_path": _input_value(value, "file_path")}
                    )
                elif name == "Bash":
                    activity.append(
                        {"type": "bash", "command": _input_value(value, "command")}
                    )
                else:
                    activity.append(
                        {
                            "type": "tool_use",
                            "name": name,
                            "input": _bounded_tool_input(value),
                        }
                    )
            elif block_type in ("Edit", "Write", "Bash"):
                key = {"Edit": "file_path", "Write": "file_path", "Bash": "command"}[
                    block_type
                ]
                activity.append(
                    {"type": block_type.lower(), key: _input_value(value, key)}
                )
    return activity


class ClaudeProcess:
    """Own the CLI and serialize turns sent through its JSONL stream."""

    def __init__(self, workspace=None, executable="claude"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        self.process = None
        self.init_event = None
        self.fatal_error = None
        self.session_id = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.current_result = None
        self._stdout_queue = None

    def ready(self):
        with self.process_lock:
            # The readiness probe runs before its first turn. Do not make it wait
            # for a lazily spawned CLI, but keep real workspace and fatal failures
            # unhealthy so a bad base is never snapshotted as ready.
            return os.path.isdir(self.workspace) and self.fatal_error is None

    def _configure_git(self):
        name = os.environ.get("EMBER_GIT_USER_NAME")
        email = os.environ.get("EMBER_GIT_USER_EMAIL")
        if not name or not email:
            raise StartupError(
                "EMBER_GIT_USER_NAME and EMBER_GIT_USER_EMAIL are required"
            )
        for key, value in (("user.name", name), ("user.email", email)):
            completed = subprocess.run(
                ["git", "config", "--global", key, value],
                cwd=self.workspace,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", "replace").strip()
                raise StartupError("git config failed for %s: %s" % (key, detail))

    def _spawn(self, session_id=None):
        if self.fatal_error is not None:
            raise StartupError(self.fatal_error)
        if not os.path.isdir(self.workspace):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        self._configure_git()
        # The microVM is the security boundary. The shim and CLI run as root inside the guest
        # (apko's run-as: 65532 is ignored on raw Firecracker boot, per review). In-guest
        # permission prompts add no containment that the VM boundary does not already provide.
        # There is no human on the other end of a prompt, by construction. So permission_mode
        # is bypassPermissions; future callers can override via EMBER_PERMISSION_MODE to tighten it.
        permission_mode = os.environ.get(PERMISSION_MODE_ENV, DEFAULT_PERMISSION_MODE)
        command = [
            self.executable,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(["--append-system-prompt", VOICE_PROMPT])
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
        with self.process_lock:
            self.process = process
            self.init_event = None
            self._stdout_queue = queue.Queue()
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_stdout,
            args=(process, self._stdout_queue),
            daemon=True,
        ).start()
        while True:
            try:
                raw = self._read_output(process, INIT_READ_TIMEOUT)
            except TimeoutError:
                self._timeout_interrupt(process, INIT_READ_TIMEOUT, "initialization")
                raise StartupError("timed out waiting for Claude initialization")
            if raw is None:
                break
            event = self._parse_line(raw)
            if (
                event
                and event.get("type") == "system"
                and event.get("subtype") == "init"
            ):
                self.init_event = event
                actual_session_id = event.get("session_id")
                if isinstance(actual_session_id, str) and actual_session_id:
                    self.session_id = actual_session_id
                elif session_id:
                    self.session_id = session_id
                if event.get("apiKeySource") != "none":
                    message = "apiKeySource must be none, got %r" % event.get(
                        "apiKeySource"
                    )
                    self.fatal_error = message
                    self._close_process(kill=True)
                    raise StartupError(message)
                return
        code = process.poll()
        self._close_process(kill=False)
        raise RuntimeError("claude exited before init, exit code %s" % code)

    @staticmethod
    def _pump_stdout(process, output_queue):
        try:
            for raw in process.stdout:
                output_queue.put(raw)
        finally:
            output_queue.put(None)

    def _read_output(self, process, timeout):
        with self.process_lock:
            output_queue = self._stdout_queue if process is self.process else None
        if output_queue is None:
            return None
        try:
            return output_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc

    @staticmethod
    def _parse_line(raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _close_process(self, kill=False):
        with self.process_lock:
            process = self.process
            self.process = None
            self._stdout_queue = None
        if process is None:
            return
        if kill and process.poll() is None:
            process.kill()
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        process.wait()
        _managed_child_pids.discard(process.pid)
        _reap_orphans()

    def turn(self, message, session_id=None):
        with self.turn_lock:
            with self.process_lock:
                process = self.process
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            if process is None or process.poll() is not None:
                if process is not None:
                    self._close_process(kill=False)
                # A request without an id resumes the last session after an
                # interrupt or relight instead of silently creating a new one.
                self._spawn(session_id or self.session_id)
                process = self.process
            if not self.ready():
                raise StartupError(self.fatal_error or "shim not ready")
            self.current_result = None
            process.stdin.write(
                _json_line(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": message}],
                        },
                    }
                )
            )
            process.stdin.flush()
            events = []
            while True:
                try:
                    raw = self._read_output(process, TURN_READ_TIMEOUT)
                except TimeoutError:
                    self._timeout_interrupt(process, TURN_READ_TIMEOUT, "turn output")
                    raise RuntimeError(
                        "timed out waiting for Claude output after %s seconds"
                        % TURN_READ_TIMEOUT
                    )
                if raw is None:
                    code = process.poll()
                    self._close_process(kill=False)
                    raise RuntimeError(
                        "claude crashed during turn, exit code %s" % code
                    )
                event = self._parse_line(raw)
                if event is None:
                    continue
                events.append(event)
                if event.get("type") == "result":
                    self.current_result = event
                    record = dict(event)
                    record["voice"] = voice_summary(event.get("result", ""))
                    record["activity"] = activity_from_events(events)
                    return record

    def _timeout_interrupt(self, process, timeout, phase):
        sys.stderr.write(
            "ember-claude-shim: %s timed out after %s seconds, sending SIGINT\n"
            % (phase, timeout)
        )
        sys.stderr.flush()
        if process.poll() is None:
            self.interrupt(timeout=INTERRUPT_TIMEOUT)

    def interrupt(self, timeout=INTERRUPT_TIMEOUT):
        with self.process_lock:
            process = self.process
        if process is None or process.poll() is not None:
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            sys.stderr.write(
                "ember-claude-shim: forcefully killing CLI after SIGINT+timeout "
                "(%s seconds exceeded)\n" % timeout
            )
            sys.stderr.flush()
            process.kill()
            process.wait()
        result = self.current_result or {}
        reason = result.get("terminal_reason", "user_interrupt")
        self._close_process(kill=False)
        return {"terminal_reason": reason, "killed": timed_out, "timeout": timed_out}


class RequestHandler(http.server.BaseHTTPRequestHandler):
    manager = None

    def log_message(self, *_args):
        pass

    def _send(self, status, value):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == HEALTHZ_PATH:
            self._send(200, {"status": "ok"})
        elif self.path == READY_PATH:
            ready = self.manager.ready()
            self._send(200 if ready else 503, {"ready": ready})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == INTERRUPT_PATH:
            self._send(200, self.manager.interrupt())
            return
        if self.path not in (TURN_PATH, CLOCK_PATH):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._send(400, {"error": "Content-Length must be an integer"})
            return
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            self._send(
                413, {"error": "request body exceeds %d bytes" % MAX_REQUEST_BODY_BYTES}
            )
            return
        raw = self.rfile.read(length)
        if self.path == CLOCK_PATH:
            self._set_clock(raw)
            return
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._send(400, {"error": "message must not be empty"})
            return
        try:
            record = self.manager.turn(message, payload.get("session_id"))
        except SessionConflictError as exc:
            self._send(409, {"error": str(exc)})
        except StartupError as exc:
            sys.stderr.write(str(exc) + "\n")
            sys.stderr.flush()
            self._send(503, {"error": str(exc)})
        except Exception as exc:
            self._send(422, {"error": str(exc)})
        else:
            self._send(200, record)

    def _set_clock(self, raw):
        try:
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                payload = payload.get("epoch_ms", payload.get("timestamp"))
            if isinstance(payload, bool) or not isinstance(payload, (int, float)):
                raise ValueError("epoch milliseconds required")
            epoch_ms = int(payload)
            if epoch_ms <= 0:
                raise ValueError("epoch milliseconds must be positive")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._send(400, {"error": "invalid clock timestamp"})
            return
        try:
            time.clock_settime(time.CLOCK_REALTIME, epoch_ms / 1000.0)
        except (AttributeError, OSError, ValueError) as exc:
            sys.stderr.write("ember-claude-shim: clock update failed: %s\n" % exc)
            sys.stderr.flush()
            self._send(500, {"error": "could not set guest clock"})
            return
        self._send(200, {"epoch_ms": epoch_ms})


def make_handler(manager):
    return type("ClaudeRequestHandler", (RequestHandler,), {"manager": manager})


class VsockHTTPServer(http.server.ThreadingHTTPServer):
    address_family = getattr(socket, "AF_VSOCK", -1)
    allow_reuse_address = False

    def server_bind(self):
        self.socket.bind(self.server_address)
        cid, port = self.socket.getsockname()
        self.server_address = (cid, port)
        self.server_name = "vsock"
        self.server_port = port


def build_server(manager):
    port = int(os.environ.get("EMBER_HTTP_PORT", str(GUEST_HTTP_PORT)))
    return VsockHTTPServer(
        (getattr(socket, "VMADDR_CID_ANY", 0xFFFFFFFF), port),
        make_handler(manager),
    )


def main():
    install_child_reaper()
    manager = ClaudeProcess()
    server = build_server(manager)
    sys.stderr.write(
        "ember-claude-shim: listening on vsock port %s\n" % server.server_port
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        manager._close_process(kill=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
