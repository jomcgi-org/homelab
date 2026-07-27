#!/usr/bin/python3
"""HTTP over vsock shim for a long-lived Claude Code CLI session."""

import http.server
import json
import os
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
DEFAULT_WORKSPACE = "/workspace"
VOICE_PROMPT = (
    "End every response with a single line: <voice>One or two plain sentences, "
    "no markdown, that a person could hear read aloud: what you did and anything "
    "you need from them.</voice>"
)


class StartupError(Exception):
    pass


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


def activity_from_events(events):
    activity = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []) or []:
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
                    activity.append({"type": "tool_use", "name": name, "input": value})
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
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.current_result = None

    def ready(self):
        with self.process_lock:
            process_alive = self.process is not None and self.process.poll() is None
            return (
                os.path.isdir(self.workspace)
                and self.init_event is not None
                and self.init_event.get("apiKeySource") == "none"
                and self.fatal_error is None
                and process_alive
            )

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
        if not os.path.isdir(self.workspace):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        self._configure_git()
        command = [
            self.executable,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
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
            self.fatal_error = None
        while process.poll() is None:
            raw = process.stdout.readline()
            if not raw:
                break
            event = self._parse_line(raw)
            if (
                event
                and event.get("type") == "system"
                and event.get("subtype") == "init"
            ):
                self.init_event = event
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
    def _parse_line(raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _close_process(self, kill=False):
        with self.process_lock:
            process = self.process
            self.process = None
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

    def turn(self, message, session_id=None):
        with self.turn_lock:
            with self.process_lock:
                process = self.process
            if process is None or process.poll() is not None:
                self._spawn(session_id)
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
                raw = process.stdout.readline()
                if not raw:
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

    def interrupt(self, timeout=5.0):
        with self.process_lock:
            process = self.process
        if process is None or process.poll() is not None:
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        process.send_signal(signal.SIGINT)
        deadline = time.time() + timeout
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.01)
        timed_out = process.poll() is None
        if timed_out:
            process.kill()
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
        if self.path != TURN_PATH:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._send(400, {"error": "message must not be empty"})
            return
        try:
            record = self.manager.turn(message, payload.get("session_id"))
        except StartupError as exc:
            sys.stderr.write(str(exc) + "\n")
            sys.stderr.flush()
            self._send(503, {"error": str(exc)})
        except Exception as exc:
            self._send(422, {"error": str(exc)})
        else:
            self._send(200, record)


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
