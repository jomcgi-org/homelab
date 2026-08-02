#!/usr/bin/python3
"""HTTP over vsock shim for a long-lived Claude Code CLI session."""

import http.server
import collections
import json
import math
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
EGRESS_LOCALHOST = "127.0.0.1"
EGRESS_PORT_ENV = "EMBER_EGRESS_PORT"
DEFAULT_EGRESS_PORT = 1024
VSOCK_EGRESS_CID = 2
VSOCK_EGRESS_PORT = 1025
VSOCK_ADDRESS_FAMILY = getattr(socket, "AF_VSOCK", -1)
HEALTHZ_PATH = "/shim/healthz"
READY_PATH = "/shim/ready"
TURN_PATH = "/shim/turn"
INTERRUPT_PATH = "/shim/interrupt"
CLOCK_PATH = "/shim/clock"
DEFAULT_WORKSPACE = "/workspace"
MAX_REQUEST_BODY_BYTES = 1 << 20
MAX_TOOL_INPUT_BYTES = 4096
# Cap on the proxy request head the egress forwarder reads before it knows the
# destination. Generous for real headers, bounded so a client that never sends
# the terminating blank line cannot grow this buffer without limit.
MAX_PROXY_HEAD_BYTES = 64 << 10
INIT_READ_TIMEOUT_ENV = "EMBER_INIT_READ_TIMEOUT"
DEFAULT_INIT_READ_TIMEOUT = 90.0
# Initialization waits inside a turn, so this must stay below the CP per-invoke
# budget (spec.invocation.timeoutSeconds, currently 900 for claude-runtime),
# below TURN_READ_TIMEOUT's 600 second role, and generous enough for the cold
# first init of the 262MB Bun binary in a microVM. BuildBase also uses this
# through /shim/ready and has its own generous budget.


def _read_init_timeout():
    """Return the positive finite init timeout from the environment."""
    try:
        value = float(os.environ.get(INIT_READ_TIMEOUT_ENV, DEFAULT_INIT_READ_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_INIT_READ_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_INIT_READ_TIMEOUT
    return value


INIT_READ_TIMEOUT = _read_init_timeout()
# Per-event inactivity timeout for the read loop in turn(). Resets on every
# stream event, so a turn emitting steady output can run far longer than this.
# Sized to span a single silent tool call: the CLI emits nothing while a Bash
# tool executes, so this must exceed the slowest realistic in-guest command
# (a build or test run), not the slowest turn. Its job is detecting a genuinely
# wedged CLI. The total-duration bound is enforced separately by the caller
# (read_timeout in projects/monolith/agent_sessions/transport.py), and this
# value must stay comfortably BELOW that one so the inner watchdog fires first
# and reports a specific error, rather than the caller timing out generically.
TURN_READ_TIMEOUT = 600.0
INTERRUPT_TIMEOUT = 30.0
CLI_PROBE_TIMEOUT = 10.0
PERMISSION_MODE_ENV = "EMBER_PERMISSION_MODE"
DEFAULT_PERMISSION_MODE = "bypassPermissions"
CLI_UID_ENV = "EMBER_CLI_UID"
CLI_GID_ENV = "EMBER_CLI_GID"
DEFAULT_CLI_UID = 65532
DEFAULT_CLI_GID = 65532
VOICE_PROMPT = (
    "End every response with a single line: <voice>One or two plain sentences, "
    "no markdown, that a person could hear read aloud: what you did and anything "
    "you need from them.</voice>"
)


def _cli_privilege_kwargs():
    """Return uid/gid kwargs only when the shim itself is running as root."""
    if os.geteuid() != 0:
        return {}
    return {
        "user": int(os.environ.get(CLI_UID_ENV, str(DEFAULT_CLI_UID))),
        "group": int(os.environ.get(CLI_GID_ENV, str(DEFAULT_CLI_GID))),
    }


def _truncate_ring_for_error(ring, max_len=1500):
    """Truncate the ring buffer for inclusion in an error message."""
    if not ring:
        return ""
    content = "\n".join(ring)
    if len(content) > max_len:
        content = content[:max_len] + "... (truncated)"
    return content


def _probe_cli_startup(executable):
    """Run CLI --version as a startup probe, log result, never fail."""
    try:
        proc = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CLI_PROBE_TIMEOUT,
            text=True,
            **_cli_privilege_kwargs(),
        )
        stdout = proc.stdout[:200] if proc.stdout else ""
        stderr = proc.stderr[:200] if proc.stderr else ""
        msg = "ember-claude-shim: cli-probe: exit=%d stdout=%r stderr=%r\n" % (
            proc.returncode,
            stdout,
            stderr,
        )
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception as exc:
        sys.stderr.write("ember-claude-shim: cli-probe failed: %s\n" % exc)
        sys.stderr.flush()


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


class VsockEgressForwarder:
    """Forward each accepted local TCP connection to one host vsock tunnel."""

    def __init__(self, port=DEFAULT_EGRESS_PORT):
        self.port = port
        self._listener = None
        self._accept_thread = None
        self._closed = threading.Event()

    def listen(self):
        if self._listener is not None:
            raise RuntimeError("egress forwarder is already listening")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((EGRESS_LOCALHOST, self.port))
        listener.listen()
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def close(self):
        self._closed.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=1)
            self._accept_thread = None

    def _accept_loop(self):
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError as exc:
                if not self._closed.is_set():
                    sys.stderr.write(
                        "ember-claude-shim: egress accept failed: %s\n" % exc
                    )
                    sys.stderr.flush()
                return
            threading.Thread(target=self._forward, args=(client,), daemon=True).start()

    @staticmethod
    def _copy(source, destination):
        try:
            while True:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination.sendall(data)
        except OSError:
            return
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    @staticmethod
    def _read_proxy_request(client):
        """Read the proxy request head and return (host_port, leftover, is_connect).

        The CLI treats this listener as an ordinary HTTP proxy because that is
        what HTTPS_PROXY means, so it opens with either a CONNECT for a TLS
        origin or an absolute-URI request for a plain one. The host-side lane
        speaks neither: it wants a single "host:port\\n" preamble and raw bytes
        after it. This reads just far enough to learn the destination.

        Returns (None, b"", False) when the head is malformed or oversized, which
        the caller answers with an error rather than guessing a destination.
        """
        head = b""
        while b"\r\n\r\n" not in head:
            if len(head) > MAX_PROXY_HEAD_BYTES:
                return None, b"", False
            chunk = client.recv(4096)
            if not chunk:
                return None, b"", False
            head += chunk
        raw_head, _, leftover = head.partition(b"\r\n\r\n")
        lines = raw_head.split(b"\r\n")
        parts = lines[0].split()
        if len(parts) < 2:
            return None, b"", False
        method, target = parts[0].upper(), parts[1].decode("latin-1")
        if method == b"CONNECT":
            # "CONNECT host:443 HTTP/1.1": the target IS the destination, and the
            # head is consumed here because the client expects a proxy response
            # before it starts its TLS handshake.
            return target, leftover, True
        # An absolute-URI request ("GET http://host/path HTTP/1.1"). Take the
        # destination from the Host header, then replay the WHOLE head upstream:
        # an origin server must accept an absolute-URI request line, so no
        # rewriting is needed and none is attempted.
        host_port = None
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if sep and name.strip().lower() == b"host":
                host_port = value.strip().decode("latin-1")
                break
        if not host_port:
            return None, b"", False
        if ":" not in host_port:
            host_port += ":80"
        return host_port, raw_head + b"\r\n\r\n" + leftover, False

    def _forward(self, client):
        upstream = None
        try:
            host_port, pending, is_connect = self._read_proxy_request(client)
            if host_port is None:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            upstream = socket.socket(VSOCK_ADDRESS_FAMILY, socket.SOCK_STREAM)
            upstream.connect((VSOCK_EGRESS_CID, VSOCK_EGRESS_PORT))
            # The one-line preamble the host lane parses before it dials, and the
            # only thing this forwarder ever writes on the guest's behalf.
            upstream.sendall(("%s\n" % host_port).encode("latin-1"))
            if is_connect:
                # The tunnel is established as far as the client is concerned; the
                # sidecar reports a dial failure by closing, which surfaces to the
                # CLI as a dropped connection rather than a proxy error, matching
                # how any CONNECT proxy behaves once it has answered.
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if pending:
                upstream.sendall(pending)
            copies = [
                threading.Thread(target=self._copy, args=(client, upstream)),
                threading.Thread(target=self._copy, args=(upstream, client)),
            ]
            for copy_thread in copies:
                copy_thread.start()
            for copy_thread in copies:
                copy_thread.join()
        except OSError as exc:
            sys.stderr.write(
                "ember-claude-shim: egress vsock connect failed: %s\n" % exc
            )
            sys.stderr.flush()
        finally:
            try:
                client.close()
            except OSError:
                pass
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class ClaudeProcess:
    """Own the CLI and serialize turns sent through its JSONL stream."""

    def __init__(self, workspace=None, executable="claude"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        _probe_cli_startup(executable)
        self.process = None
        self.init_event = None
        self.fatal_error = None
        self.session_id = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.current_result = None
        self._stdout_queue = None
        self.unparseable_lines = collections.deque(maxlen=5)
        self.stderr_lines = collections.deque(maxlen=5)
        self.parsed_events = collections.deque(maxlen=5)

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
        # The microVM is the security boundary. The shim may start as root inside the guest
        # (apko's run-as: 65532 is ignored on raw Firecracker boot, per review), so drop the
        # CLI to the runtime uid/gid only when the shim is root. In-guest
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
        egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
        proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
        child_env = os.environ.copy()
        child_env.update(
            {
                "HTTPS_PROXY": proxy_url,
                "HTTP_PROXY": proxy_url,
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            **_cli_privilege_kwargs(),
        )
        with self.process_lock:
            self.process = process
            self.init_event = None
            self._stdout_queue = queue.Queue()
            # REPLACE the rings rather than clearing them: a stale pump thread from
            # a previous process holds the old deque and would keep appending to a
            # shared one, reporting a dead process's output as this one's last words.
            # stderr gets its OWN ring because SIGINT on the timeout path always
            # emits a multi-line traceback, which in a shared maxlen deque evicts the
            # stdout dying words exactly when they matter most.
            self.unparseable_lines = collections.deque(maxlen=5)
            self.stderr_lines = collections.deque(maxlen=5)
            self.parsed_events = collections.deque(maxlen=5)
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_stdout,
            args=(process, self._stdout_queue),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump_stderr,
            args=(process, self.stderr_lines),
            daemon=True,
        ).start()
        # Note: unparseable-line stderr writes are synchronous on the read thread
        # and race the init timeout. This is fine for tens-of-lines Bun panics
        # but could turn thousands-of-lines dumps into a generic init timeout.
        init_read_timeout = _read_init_timeout()
        while True:
            try:
                raw = self._read_output(process, init_read_timeout)
            except TimeoutError:
                self._timeout_interrupt(process, init_read_timeout, "initialization")
                raise StartupError(
                    self._assemble_error_with_rings(
                        "timed out waiting for Claude initialization after %s seconds"
                        % init_read_timeout
                    )
                )
            if raw is None:
                break
            event = self._parse_line(raw)
            if event is None:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
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
            else:
                try:
                    raw_str = raw.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    raw_str = repr(raw[:200])
                if len(raw_str) > 200:
                    raw_str = raw_str[:200]
                self.parsed_events.append("event: " + raw_str)
        code = process.poll()
        self._close_process(kill=False)
        error_msg = "claude exited before init, exit code %s" % code
        raise RuntimeError(self._assemble_error_with_rings(error_msg))

    @staticmethod
    def _pump_stdout(process, output_queue):
        try:
            for raw in process.stdout:
                output_queue.put(raw)
        finally:
            output_queue.put(None)

    def _pump_stderr(self, process, ring):
        """Read stderr lines onto the console and into the given ring.

        The ring is passed in rather than read off self, so a pump left running
        for a previous process cannot append into the current process's ring.
        """
        try:
            # readline, not file iteration: iteration read-ahead buffers a pipe,
            # delaying lines until the buffer fills or EOF; readline yields each
            # line as the CLI writes it.
            for raw in iter(process.stderr.readline, b""):
                try:
                    line_str = raw.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    line_str = repr(raw[:2000])
                if len(line_str) > 2000:
                    line_str = line_str[:2000]
                sys.stderr.write("ember-claude-shim: cli-stderr: %s\n" % line_str)
                sys.stderr.flush()
                ring.append(line_str)
        except Exception:
            pass

    def _read_output(self, process, timeout):
        with self.process_lock:
            output_queue = self._stdout_queue if process is self.process else None
        if output_queue is None:
            return None
        try:
            return output_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc

    def _parse_line(self, raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            try:
                line_str = raw[:2000].decode("utf-8", errors="replace").rstrip("\n")
            except Exception:
                line_str = repr(raw[:2000])
            sys.stderr.write("ember-claude-shim: cli-stdout: %s\n" % line_str)
            sys.stderr.flush()
            self.unparseable_lines.append(line_str)
            return None

    def _assemble_error_with_rings(self, base_msg):
        """Append truncated CLI output, stderr, and parsed events to an error."""
        sections = [
            ("CLI output:", self.unparseable_lines),
            ("CLI stderr:", self.stderr_lines),
            ("Parsed events:", self.parsed_events),
        ]
        error_msg = base_msg
        for label, ring in sections:
            content = _truncate_ring_for_error(ring)
            if content:
                error_msg += "\n%s\n%s" % (label, content)
        return error_msg

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
                    turn_read_timeout = TURN_READ_TIMEOUT
                    raw = self._read_output(process, turn_read_timeout)
                except TimeoutError:
                    self._timeout_interrupt(process, turn_read_timeout, "turn output")
                    raise RuntimeError(
                        "timed out waiting for Claude output after %s seconds"
                        % turn_read_timeout
                    )
                if raw is None:
                    code = process.poll()
                    self._close_process(kill=False)
                    error_msg = "claude crashed during turn, exit code %s" % code
                    raise RuntimeError(self._assemble_error_with_rings(error_msg))
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
    egress_port = int(os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT)))
    egress = VsockEgressForwarder(egress_port)
    server = None
    try:
        egress.listen()
        sys.stderr.write(
            "ember-claude-shim: egress listening on %s:%s\n"
            % (EGRESS_LOCALHOST, egress.port)
        )
        sys.stderr.flush()
        server = build_server(manager)
        sys.stderr.write(
            "ember-claude-shim: listening on vsock port %s\n" % server.server_port
        )
        sys.stderr.flush()
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        egress.close()
        manager._close_process(kill=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
