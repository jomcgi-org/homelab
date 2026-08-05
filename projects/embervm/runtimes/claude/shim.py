#!/usr/bin/python3
"""HTTP over vsock shim for a long-lived Claude Code CLI session."""

import http.server
import base64
import collections
import json
import math
import os
import queue
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request


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
CODEX_MODELS = {
    "luna": ("gpt-5.6-luna", "medium"),
    "terra": ("gpt-5.6-terra", "high"),
    "sol": ("gpt-5.6-sol", "high"),
}
CLAUDE_MODELS = {
    "opus": "opus",
    "sonnet": "sonnet",
    "fable": "claude-fable-5",
}
DEFAULT_CODEX_MODEL = "luna"
CODEX_SUBSCRIPTION_BASE_URL_ENV = "CODEX_SUBSCRIPTION_BASE_URL"
DEFAULT_CODEX_SUBSCRIPTION_BASE_URL = "http://chatgpt.com/backend-api/"
CODEX_DUMMY_ACCOUNT_ID = "guest-subscription-account"
PI_MODELS = {
    # Must match the vLLM --served-model-name (projects/inference/deploy/
    # values.yaml); a wrong name 404s at the provider ("The model does not
    # exist"), proven live in #4252.
    "qwen": "qwen3.6-27b",
}
DEFAULT_PI_MODEL = "qwen"
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
HYDRATION_ATTEMPT_CAP = 3
# The guest path is proxied over vsock, so it is materially slower than a direct
# clone. A partial clone of this repo is approximately 67 MB.
GIT_CLONE_TIMEOUT_SECONDS = 300
PERMISSION_MODE_ENV = "EMBER_PERMISSION_MODE"
DEFAULT_PERMISSION_MODE = "bypassPermissions"
CLI_UID_ENV = "EMBER_CLI_UID"
CLI_GID_ENV = "EMBER_CLI_GID"
DEFAULT_CLI_UID = 65532
DEFAULT_CLI_GID = 65532
PERSISTENCE_MOUNT_PATH_ENV = "EMBER_PERSISTENCE_MOUNT_PATH"
DEFAULT_PERSISTENCE_MOUNT_PATH = "/session"
GUEST_INIT_PATH = "/usr/local/bin/ember-runtime-guest-init"
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


GIT_PROXY_PATH = "/tmp/ember-git-proxy"


def _write_git_proxy_helper():
    """Install the stdlib-only git proxy used by session guests."""
    proxy = r"""#!/usr/bin/python3
import os
import socket
import sys
import threading

EGRESS_LOCALHOST = "127.0.0.1"
EGRESS_PORT_ENV = "EMBER_EGRESS_PORT"
DEFAULT_EGRESS_PORT = 1024


def _pump_stdin_to_socket(source, sock):
    try:
        while True:
            # read1, NOT read: BufferedReader.read(n) blocks until it has all n
            # bytes or EOF, and git's protocol is request/response in messages of
            # a few hundred bytes. read(65536) therefore holds each request until
            # 64 KiB accumulates or the stream closes, stalling every negotiation
            # round trip and turning a 4s clone into a timeout. read1 returns what
            # is available after one syscall.
            data = source.read1(65536)
            if not data:
                break
            sock.sendall(data)
    except OSError:
        pass
    finally:
        # Half-close: tell the server the request stream is done while the
        # response direction keeps flowing.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _pump_socket_to_stdout(sock, destination):
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            destination.write(data)
            destination.flush()
    except OSError:
        pass


def main():
    if len(sys.argv) != 3:
        return 2
    host, port = sys.argv[1:]
    try:
        egress_port = int(os.environ.get(EGRESS_PORT_ENV, DEFAULT_EGRESS_PORT))
        sock = socket.create_connection((EGRESS_LOCALHOST, egress_port))
        sock.sendall(("CONNECT %s:%s HTTP/1.1\r\n\r\n" % (host, port)).encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                return 1
            response += chunk
            if len(response) > 65536:
                return 1
        if not response.startswith(b"HTTP/1.1 200"):
            return 1
        threads = [
            threading.Thread(
                target=_pump_stdin_to_socket, args=(sys.stdin.buffer, sock)
            ),
            threading.Thread(
                target=_pump_socket_to_stdout, args=(sock, sys.stdout.buffer)
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return 0
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
"""
    with open(GIT_PROXY_PATH, "w") as stream:
        stream.write(proxy)
    os.chmod(GIT_PROXY_PATH, 0o755)


def _ensure_cli_dir(path):
    """Create a CLI state dir the CLI PROCESS can write into.

    The shim may run as root while the CLI is dropped to the runtime uid, so a
    bare makedirs here leaves a root-owned dir the CLI cannot create subdirs
    in (observed live: pi dying with EACCES on mkdir /workspace/.pi/sessions).
    Chown to the same uid/gid the CLI is spawned with whenever the shim is
    root; a non-root shim already creates dirs the CLI owns.
    """
    os.makedirs(path, exist_ok=True)
    kwargs = _cli_privilege_kwargs()
    if kwargs:
        os.chown(path, kwargs["user"], kwargs["group"])


def _persistence_mount_path():
    """Return the configured persistence mount path."""
    path = os.environ.get(PERSISTENCE_MOUNT_PATH_ENV)
    if path:
        return path

    try:
        with open("/proc/cmdline") as stream:
            cmdline = stream.read()
    except OSError:
        cmdline = ""
    for token in cmdline.split():
        if token.startswith("ember.volume_mount="):
            configured_path = token.split("=", 1)[1]
            if configured_path:
                return configured_path

    # The CR defines this durable volume path, but keep a default for non-init
    # startup paths that do not provide the boot argument.
    return DEFAULT_PERSISTENCE_MOUNT_PATH


def _ensure_persistence_mountpoint_writable(path):
    """Make the persistence mountpoint writable by the CLI process."""
    kwargs = _cli_privilege_kwargs()
    if not kwargs:
        return

    try:
        ownership = os.stat(path)
    except OSError:
        return

    if ownership.st_uid == kwargs["user"] and kwargs["group"] == ownership.st_gid:
        return

    # Only the mountpoint is chowned. Its contents can be a 10 GiB volume, so
    # recursing would put unbounded work on every boot.
    #
    # A failure here is deliberately NOT swallowed. The bug this fixes was
    # invisible precisely because the CLI fell back to ephemeral storage and
    # the session looked healthy while persisting nothing, so a mountpoint the
    # CLI still cannot write is worth failing loudly on (#4291).
    os.chown(path, kwargs["user"], kwargs["group"])


def ensure_workspace_volume():
    """Ensure the session volume is mounted before the first real turn.

    The guest-init binary owns the privileged mount implementation. It is
    invoked after the vsock request reaches the shim, which is late enough for
    a resumed VM's per-session drive to have replaced the warm-base device.
    The Go side checks mountinfo, so this remains a no-op for cold guests and
    repeated turns.
    """
    # The image always contains guest-init. Keeping this guard makes the shim
    # library usable in host-side unit tests and in non-microVM tooling, where
    # the privileged guest helper is intentionally absent.
    if not os.path.exists(GUEST_INIT_PATH):
        return
    try:
        subprocess.run(
            [GUEST_INIT_PATH, "--ensure-workspace-volume"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartupError("could not ensure workspace volume: %s" % exc) from exc


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
        # A version probe reads nothing; close stdin rather than inherit the
        # shim's, so a never-EOF stdin cannot block it.
        proc = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
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


def _user_message_line(message):
    return _json_line(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": message}],
            },
        }
    )


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
        if event.get("type") == "tool_execution_start":
            name = event.get("toolName")
            value = event.get("args")
            if name == "edit":
                activity.append(
                    {"type": "edit", "file_path": _input_value(value, "path")}
                )
            elif name == "write":
                activity.append(
                    {"type": "write", "file_path": _input_value(value, "path")}
                )
            elif name == "bash":
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


class _ProgressPusher:
    """Fire-and-forget progress pusher: latest-text slot, throttled, exceptions swallowed."""

    def __init__(self, progress_token):
        self.progress_token = progress_token
        self.latest_text = ""
        self.latest_activities = []
        self.last_push_time = -1.0
        self.thread = None
        self.last_sent_text = ""
        self.last_sent_activities = []

    def push(self, text, activities=None):
        """Update the latest slot and trigger a push if throttle allows."""
        try:
            self.latest_text = text
            self.latest_activities = activities if activities is not None else []
            now = time.monotonic()
            if now - self.last_push_time >= 0.2:
                self.last_push_time = now
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(
                        target=self._do_push,
                        daemon=True,
                    )
                    self.thread.start()
        except Exception:
            # Fire-and-forget: even thread startup failures never fail the turn
            # (ADR 051 decision 6).
            pass

    def _do_push(self):
        """Push in a background thread and drain changes made during the push."""
        try:
            egress_port = int(os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT)))
            proxy_handler = urllib.request.ProxyHandler(
                {"http": "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)}
            )
            opener = urllib.request.build_opener(proxy_handler)
            url = "http://monolith.monolith.svc.cluster.local:8091/ingest/progress"
            sent_text = self.latest_text
            sent_activities = list(self.latest_activities)
            payload = {
                "partial_text": sent_text[-65536:]
                if len(sent_text) > 65536
                else sent_text,
                "activities": sent_activities,
            }
            data = json.dumps(payload).encode("utf-8")
            while len(data) >= 262144 and (sent_activities or payload["partial_text"]):
                if sent_activities:
                    sent_activities.pop(0)
                else:
                    payload["partial_text"] = payload["partial_text"][1000:]
                payload["activities"] = sent_activities
                data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": "Bearer %s" % self.progress_token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            response = opener.open(request, timeout=2)
            response.close()
            self.last_sent_text = sent_text
            self.last_sent_activities = sent_activities
            if (
                self.latest_text != self.last_sent_text
                or self.latest_activities != self.last_sent_activities
            ):
                self.last_push_time = time.monotonic()
                remaining = 0.2 - (time.monotonic() - self.last_push_time)
                if remaining > 0:
                    time.sleep(remaining)
                self.thread = threading.Thread(
                    target=self._do_push,
                    daemon=True,
                )
                self.thread.start()
        except Exception:
            # Fire-and-forget: all exceptions swallowed (decision 6, ADR 051).
            pass

    def stop(self):
        """Drain and stop. Waits up to 3 seconds for in-flight push to complete."""
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)


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
        self.model = None
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

    def _spawn(self, session_id=None, first_message=None, model=None):
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
        command.extend(["--include-partial-messages"])
        if model is not None:
            command.extend(["--model", CLAUDE_MODELS.get(model, model)])
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
        if first_message is not None:
            process.stdin.write(_user_message_line(first_message))
            process.stdin.flush()
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
                self.model = model
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
        # Bounded: most callers close an already-exited process (wait returns
        # instantly), but the model-change respawn closes a LIVE CLI, and an
        # unbounded wait there would hang the turn under turn_lock if the CLI
        # ignores stdin EOF (interrupt() treats the same wait as unsafe).
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _managed_child_pids.discard(process.pid)
        _reap_orphans()

    def turn(
        self,
        message,
        session_id=None,
        model=None,
        progress_token=None,
    ):
        with self.turn_lock:
            with self.process_lock:
                process = self.process
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            model_changed = model is not None and model != self.model
            if process is not None and process.poll() is None and model_changed:
                self._close_process(kill=False)
                self._spawn(
                    self.session_id or session_id,
                    first_message=message,
                    model=model,
                )
                process = self.process
                message_sent = True
            else:
                message_sent = False
            # After a model-change respawn the first_message is already in
            # flight; falling into this branch would _spawn again and deliver
            # (and bill) the same turn twice if the fresh CLI died between
            # init and this poll.
            if not message_sent and (process is None or process.poll() is not None):
                if process is not None:
                    self._close_process(kill=False)
                # A request without an id resumes the last session after an
                # interrupt or relight instead of silently creating a new one.
                self._spawn(
                    session_id or self.session_id,
                    first_message=message,
                    model=model,
                )
                process = self.process
                message_sent = True
            if not self.ready():
                raise StartupError(self.fatal_error or "shim not ready")
            self.current_result = None
            if not message_sent:
                process.stdin.write(_user_message_line(message))
                process.stdin.flush()
            events = []
            accumulated_text = ""
            current_message_buffer = ""
            cached_activities = []
            activities_are_stale = True
            pusher = _ProgressPusher(progress_token) if progress_token else None
            try:
                while True:
                    try:
                        turn_read_timeout = TURN_READ_TIMEOUT
                        raw = self._read_output(process, turn_read_timeout)
                    except TimeoutError:
                        self._timeout_interrupt(
                            process, turn_read_timeout, "turn output"
                        )
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
                    if event.get("type") == "stream_event":
                        stream_event = event.get("event")
                        delta = (
                            stream_event.get("delta")
                            if isinstance(stream_event, dict)
                            and stream_event.get("type") == "content_block_delta"
                            else None
                        )
                        if (
                            isinstance(delta, dict)
                            and delta.get("type") == "text_delta"
                        ):
                            text = delta.get("text", "")
                            if isinstance(text, str):
                                current_message_buffer += text
                                if pusher:
                                    pusher.push(
                                        accumulated_text + current_message_buffer,
                                        cached_activities,
                                    )
                    elif event.get("type") == "assistant":
                        message_event = event.get("message")
                        if (
                            isinstance(message_event, dict)
                            and message_event.get("role") == "assistant"
                        ):
                            content = message_event.get("content")
                            if isinstance(content, list):
                                for block in content:
                                    if (
                                        isinstance(block, dict)
                                        and block.get("type") == "text"
                                    ):
                                        text = block.get("text")
                                        if isinstance(text, str):
                                            accumulated_text += text
                            current_message_buffer = ""
                            activities_are_stale = True
                    elif event.get("type") == "tool_execution_start":
                        activities_are_stale = True
                    if activities_are_stale:
                        cached_activities = activity_from_events(events)[-300:]
                        activities_are_stale = False
                    if event.get("type") == "assistant" and pusher:
                        pusher.push(
                            accumulated_text + current_message_buffer,
                            cached_activities,
                        )
                    if event.get("type") == "result":
                        if pusher:
                            pusher.push(
                                accumulated_text + current_message_buffer,
                                cached_activities,
                            )
                        self.current_result = event
                        record = dict(event)
                        record["voice"] = voice_summary(event.get("result", ""))
                        record["activity"] = activity_from_events(events)
                        return record
            finally:
                if pusher:
                    pusher.stop()

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


class CodexProcess:
    """Own one long-lived Codex app-server process and bind threads lazily."""

    def __init__(self, workspace=None, executable="codex"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        self.process = None
        self.session_id = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = None
        self._stdout_queue = None
        self._rpc_id = 0
        self._server_threads = set()
        self._turn_id = None
        self._turn_done = threading.Event()
        self._turn_done.set()
        self._write_lock = threading.Lock()

    def ready(self):
        with self.process_lock:
            return os.path.isdir(self.workspace)

    def _child_env(self):
        egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
        proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
        # The CLI state dir must live under the WORKSPACE, not $HOME: the guest's
        # $HOME is on the read-only rootfs and the codex CLI refuses to start when
        # CODEX_HOME does not exist (observed live as a 422 on every codex turn).
        # The config.toml is regenerated per spawn; a stale workspace cannot pin
        # an old base URL.
        # The workspace is also where session files must sit for thread resume to
        # survive bank/relight once workspaces ride the durable volume.
        codex_home = os.path.join(self.workspace, ".codex")
        _ensure_cli_dir(codex_home)
        child_env = os.environ.copy()
        # Subscription auth is carried by the inert auth.json below. An API
        # key is not part of ChatGPT subscription mode and must not confuse
        # the CLI or the egress sidecar.
        child_env.pop("OPENAI_API_KEY", None)
        child_env.update(
            {
                "CODEX_HOME": codex_home,
                "HTTPS_PROXY": proxy_url,
                "HTTP_PROXY": proxy_url,
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        return child_env

    @staticmethod
    def _dummy_jwt():
        """A syntactically valid, far-future JWT carrying no real credential.

        The CLI decodes these tokens locally (expiry, account id), so a bare
        placeholder string fails to parse and the CLI decides it is logged
        out. The signature is nonsense on purpose: nothing verifies it here,
        and the sidecar replaces the header with the broker's real token
        before the request leaves the guest.
        """
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "exp": 4102444800,  # 2100-01-01, so the CLI never self-refreshes
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": CODEX_DUMMY_ACCOUNT_ID
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).rstrip(b"=")
        return b".".join([header, payload, b"ember"]).decode("ascii")

    @staticmethod
    def _write_auth_json(codex_home):
        # Shape mirrors a real subscription auth.json (auth_mode, a null API key,
        # JWT-shaped tokens): the CLI parses these, so a placeholder string reads
        # as logged out. The broker owns refresh; a guest refresh would consume
        # the rotating token and lock out the fleet (ADR 048 invariant), which is
        # why last_refresh sits in the future and the tokens never expire.
        token = CodexProcess._dummy_jwt()
        auth = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": token,
                "access_token": token,
                "refresh_token": token,
                "account_id": CODEX_DUMMY_ACCOUNT_ID,
            },
            "last_refresh": "2099-12-31T23:59:59Z",
        }
        with open(os.path.join(codex_home, "auth.json"), "w") as stream:
            json.dump(auth, stream)

    @staticmethod
    def _write_model_config(codex_home):
        base_url = os.environ.get(
            CODEX_SUBSCRIPTION_BASE_URL_ENV, DEFAULT_CODEX_SUBSCRIPTION_BASE_URL
        )
        provider_base_url = base_url.rstrip("/") + "/codex/"
        # Subscription backend endpoint over cleartext injection lane (token injection happens at sidecar).
        # chatgpt_base_url must be set too: the CLI's connector client (rmcp
        # transport) builds its URL from chatgpt_base_url, not from the model
        # provider base_url. Left at the https default it bypasses the sidecar's
        # injection lane, presents the guest's placeholder JWT, gets a 401, and
        # the CLI's resulting token refresh attempt aborts the turn (issue #4298).
        #
        # The model provider base_url is a different value on purpose. Codex
        # turn requests get posted to {provider base_url}/responses, and the
        # subscription backend only serves turns at /backend-api/codex/responses,
        # not at /backend-api/responses. The rmcp connector client above builds
        # from chatgpt_base_url at the backend-api root, so the two values must
        # diverge: chatgpt_base_url stays at the root, provider_base_url gets a
        # /codex/ suffix. The trailing slash on provider_base_url is load
        # bearing: it makes both plausible client join behaviors, trimming the
        # trailing slash and concatenating, or an RFC 3986 Url::join with a
        # relative segment, land on the same /codex/responses path.
        config = """model_provider = "ember-openai"
enable_codex_api_key_env = false
chatgpt_base_url = %s
sandbox_mode = "danger-full-access"
approval_policy = "never"

[model_providers.ember-openai]
name = "ember-openai"
base_url = %s
wire_api = "responses"
""" % (json.dumps(base_url), json.dumps(provider_base_url))
        with open(os.path.join(codex_home, "config.toml"), "w") as stream:
            stream.write(config)

    def _spawn(self):
        if not os.path.isdir(self.workspace):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        child_env = self._child_env()
        self._write_auth_json(child_env["CODEX_HOME"])
        self._write_model_config(child_env["CODEX_HOME"])
        process = subprocess.Popen(
            [self.executable, "app-server"],
            cwd=self.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            **_cli_privilege_kwargs(),
        )
        output_queue = queue.Queue()
        with self.process_lock:
            self.process = process
            self._stdout_queue = output_queue
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_codex_stdout, args=(process, output_queue), daemon=True
        ).start()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = threading.Thread(
            target=ClaudeProcess._pump_stderr,
            args=(self, process, self.stderr_lines),
            daemon=True,
        )
        self._stderr_thread.start()
        self._server_threads = set()
        self._turn_id = None
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "homelab-shim",
                        "title": "Homelab Shim",
                        "version": "1.0",
                    }
                },
                timeout=INIT_READ_TIMEOUT,
            )
            self._send({"jsonrpc": "2.0", "method": "initialized"})
        except Exception:
            self._close_process(kill=True)
            raise
        return process

    def _empty_stream_error(self, process):
        if process is None:
            code = None
        else:
            code = process.poll()
            if code is None:
                code = process.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        error_msg = "codex exited before turn.completed, exit code %s" % code
        stderr = _truncate_ring_for_error(self.stderr_lines)
        if stderr:
            error_msg += "\nCLI stderr:\n%s" % stderr
        return RuntimeError(error_msg)

    def _send(self, value):
        with self._write_lock:
            process = self.process
            if process is None or process.poll() is not None:
                raise self._empty_stream_error(process)
            process.stdin.write(_json_line(value))
            process.stdin.flush()

    def _request(self, method, params, timeout=TURN_READ_TIMEOUT):
        with self._write_lock:
            self._rpc_id += 1
            request_id = self._rpc_id
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            event = self._read_event(timeout)
            if event is None:
                raise self._empty_stream_error(self.process)
            if event.get("id") == request_id:
                if "error" in event:
                    raise RuntimeError(self._rpc_error(event))
                return event.get("result", {})
            self._handle_server_request(event)

    @staticmethod
    def _rpc_error(response):
        error = response.get("error")
        if isinstance(error, dict):
            return "%s: %s" % (error.get("code", "error"), error.get("message", error))
        return str(error)

    def _handle_server_request(self, event):
        if event.get("method") is None or event.get("id") is None:
            return
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": event["id"],
                    "error": {
                        "code": -32000,
                        "message": "server request denied by homelab shim",
                    },
                }
            )
        except Exception:
            pass
        sys.stderr.write("ember-claude-shim: denied Codex server request %r\n" % event)
        sys.stderr.flush()

    def _read_event(self, timeout):
        with self.process_lock:
            output_queue = self._stdout_queue
        if output_queue is None:
            return None
        try:
            raw = output_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("codex emitted invalid JSON: %s" % exc) from exc

    def _resume(self, session_id):
        params = {
            "threadId": session_id,
            "cwd": self.workspace,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        try:
            result = self._request("thread/resume", params, timeout=INIT_READ_TIMEOUT)
        except RuntimeError as error:
            raise RuntimeError(
                "unable to resume session %s: %s\n%s"
                % (session_id, error, _truncate_ring_for_error(self.stderr_lines))
            ) from error
        thread = result.get("thread", {}) if isinstance(result, dict) else {}
        thread_id = (
            thread.get("id", session_id) if isinstance(thread, dict) else session_id
        )
        self.session_id = thread_id
        self._server_threads.add(thread_id)

    def _translate_activity_event(self, event):
        if event.get("method") == "item/started":
            item = event.get("params", {}).get("item", {})
            if isinstance(item, dict) and item.get("type") in (
                "commandExecution",
                "command_execution",
            ):
                return {
                    "type": "tool_execution_start",
                    "toolName": "bash",
                    "args": {"command": item.get("command", "")},
                }
        return event

    @staticmethod
    def _pump_codex_stdout(process, output_queue):
        try:
            for raw in process.stdout:
                output_queue.put(raw)
        finally:
            output_queue.put(None)

    def turn(
        self,
        message,
        session_id=None,
        model=DEFAULT_CODEX_MODEL,
        progress_token=None,
    ):
        with self.turn_lock:
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            requested_session = session_id or self.session_id
            with self.process_lock:
                process = self.process
            if process is None or process.poll() is not None:
                self._close_process(kill=False)
                process = self._spawn()
                requested_session = session_id or self.session_id
            if requested_session and (
                requested_session not in self._server_threads
                or requested_session != self.session_id
            ):
                self._resume(requested_session)
            elif not requested_session:
                result = self._request(
                    "thread/start",
                    {
                        "cwd": self.workspace,
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                    },
                )
                thread = result.get("thread", {}) if isinstance(result, dict) else {}
                self.session_id = thread.get("id") if isinstance(thread, dict) else None
                self._server_threads.add(self.session_id)
            with self._write_lock:
                self._turn_done.clear()
                self._turn_id = None
                self._rpc_id += 1
                request_id = self._rpc_id
            model_name, effort = CODEX_MODELS.get(
                model, CODEX_MODELS[DEFAULT_CODEX_MODEL]
            )
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "turn/start",
                    "params": {
                        "threadId": self.session_id,
                        "input": [{"type": "text", "text": message}],
                        "model": model_name,
                        "effort": effort,
                        "sandboxPolicy": {"type": "dangerFullAccess"},
                        "approvalPolicy": "never",
                        "cwd": self.workspace,
                    },
                }
            )
            result_text = ""
            usage = {}
            events = []
            accumulated_text = ""
            cached_activities = []
            activities_are_stale = True
            try:
                pusher = _ProgressPusher(progress_token) if progress_token else None
            except Exception:
                pusher = None
            try:
                while True:
                    try:
                        event = self._read_event(TURN_READ_TIMEOUT)
                    except TimeoutError as exc:
                        self._close_process(kill=True)
                        raise RuntimeError(
                            "timed out waiting for Codex output after %s seconds"
                            % TURN_READ_TIMEOUT
                        ) from exc
                    if event is None:
                        raise self._empty_stream_error(process)
                    if event.get("id") == request_id:
                        if "error" in event:
                            raise RuntimeError(self._rpc_error(event))
                        continue
                    self._handle_server_request(event)
                    event_type = event.get("method")
                    params = event.get("params", {})
                    legacy_event = self._translate_activity_event(event)
                    events.append(legacy_event)
                    if event_type == "turn/started":
                        self._turn_id = params.get("turn", {}).get("id")
                    elif event_type == "item/agentMessage/delta":
                        delta = params.get("delta", {})
                        text = delta.get("text") if isinstance(delta, dict) else None
                        if isinstance(text, str):
                            accumulated_text += text
                            if pusher:
                                try:
                                    pusher.push(accumulated_text, cached_activities)
                                except Exception:
                                    pass
                    elif event_type in ("item/started", "item/completed"):
                        item = params.get("item", {})
                        if isinstance(item, dict) and item.get("type") in (
                            "commandExecution",
                            "command_execution",
                        ):
                            activities_are_stale = True
                        if event_type == "item/completed" and isinstance(item, dict):
                            if item.get("type") in ("agentMessage", "agent_message"):
                                result_text = item.get("text", "")
                    elif event_type == "thread/tokenUsage/updated":
                        last = params.get("tokenUsage", {}).get("last", {})
                        usage = {
                            "input_tokens": last.get("inputTokens", 0),
                            "output_tokens": last.get("outputTokens", 0),
                            "cache_read_tokens": last.get("cachedInputTokens", 0),
                            "cache_write_tokens": last.get("cacheWriteInputTokens", 0),
                        }
                    if activities_are_stale:
                        cached_activities = activity_from_events(events)[-300:]
                        activities_are_stale = False
                        if pusher and event_type in ("item/started", "item/completed"):
                            try:
                                pusher.push(
                                    accumulated_text or result_text,
                                    cached_activities,
                                )
                            except Exception:
                                pass
                    if event_type == "turn/completed":
                        if pusher:
                            try:
                                pusher.push(
                                    accumulated_text or result_text,
                                    cached_activities,
                                )
                            except Exception:
                                pass
                        turn = params.get("turn", {})
                        status = turn.get("status", "completed")
                        if status == "failed":
                            error = turn.get("error", "Codex turn failed")
                            if isinstance(error, dict):
                                error = error.get("message", error)
                            raise RuntimeError(
                                "Codex turn failed: %s\n%s"
                                % (error, _truncate_ring_for_error(self.stderr_lines))
                            )
                        terminal_reason = (
                            "user_interrupt" if status == "interrupted" else "completed"
                        )
                        return {
                            "result": result_text,
                            "terminal_reason": terminal_reason,
                            "session_id": self.session_id,
                            "usage": usage,
                            "voice": voice_summary(result_text),
                            "activity": activity_from_events(events),
                        }
            finally:
                if pusher:
                    try:
                        pusher.stop()
                    except Exception:
                        pass
                self._turn_done.set()

    @staticmethod
    def _reap_process(process):
        try:
            process.wait()
        finally:
            _managed_child_pids.discard(process.pid)

    def interrupt(self, timeout=INTERRUPT_TIMEOUT):
        with self.process_lock:
            process = self.process
            turn_id = self._turn_id
            thread_id = self.session_id
        if (
            process is None
            or process.poll() is not None
            or self._turn_done.is_set()
            or not turn_id
        ):
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        try:
            with self._write_lock:
                self._rpc_id += 1
                request_id = self._rpc_id
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            )
        except Exception:
            self._close_process(kill=True)
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        timed_out = not self._turn_done.wait(timeout=timeout)
        if timed_out:
            self._close_process(kill=True)
        return {
            "terminal_reason": "user_interrupt",
            "killed": timed_out,
            "timeout": timed_out,
        }

    def _close_process(self, kill=False):
        with self.process_lock:
            process = self.process
            self.process = None
            self._stdout_queue = None
            self._server_threads = set()
            self._turn_id = None
        if process is None:
            return
        if kill and process.poll() is None:
            process.kill()
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _managed_child_pids.discard(process.pid)
        _reap_orphans()


class PiProcess:
    """Own one long-lived Pi RPC process and bind sessions lazily."""

    INFERENCE_BASE_URL = "http://inference.inference.svc.cluster.local:8080/v1"

    def __init__(self, workspace=None, executable="pi"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        self.process = None
        self.session_id = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = None
        self._stdout_queue = None
        self._write_lock = threading.Lock()
        self._turn_done = threading.Event()
        self._turn_done.set()
        self._in_flight = False
        self._model = None
        self._session_file = None

    def ready(self):
        with self.process_lock:
            return os.path.isdir(self.workspace)

    def _child_env(self):
        egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
        proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
        # Same constraint as the codex adapter: $HOME is read-only rootfs in the
        # guest, so pi's state dir lives under the writable workspace, which is
        # also where session files must sit to survive bank/relight.
        pi_home = os.path.join(self.workspace, ".pi")
        _ensure_cli_dir(pi_home)
        _ensure_cli_dir(os.path.join(pi_home, "agent"))
        child_env = os.environ.copy()
        child_env.update(
            {
                "PI_HOME": pi_home,
                "PI_CODING_AGENT_DIR": os.path.join(pi_home, "agent"),
                "HTTPS_PROXY": proxy_url,
                "HTTP_PROXY": proxy_url,
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        return child_env

    def _write_model_config(self, pi_home):
        agent_dir = os.path.join(pi_home, "agent")
        _ensure_cli_dir(agent_dir)
        config = {
            "providers": {
                "openai-completions": {
                    "baseUrl": self.INFERENCE_BASE_URL,
                    "api": "openai-completions",
                    "apiKey": "sk-noauth",
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                    },
                    "models": [{"id": PI_MODELS[DEFAULT_PI_MODEL]}],
                }
            }
        }
        with open(os.path.join(agent_dir, "models.json"), "w") as stream:
            json.dump(config, stream)

    def _spawn(self, model):
        if not os.path.isdir(self.workspace):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        model_name = PI_MODELS.get(model, PI_MODELS[DEFAULT_PI_MODEL])
        child_env = self._child_env()
        pi_home = child_env["PI_HOME"]
        self._write_model_config(pi_home)
        command = [
            self.executable,
            "--mode",
            "rpc",
            "--provider",
            "openai-completions",
            "--model",
            model_name,
            "--system-prompt",
            "You are a focused coding agent. " + VOICE_PROMPT,
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--tools",
            "read,bash,edit,write",
            "--session-dir",
            os.path.join(pi_home, "sessions"),
        ]
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            **_cli_privilege_kwargs(),
        )
        output_queue = queue.Queue()
        with self.process_lock:
            self.process = process
            self._stdout_queue = output_queue
            self._model = model
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_pi_stdout,
            args=(process, output_queue),
            daemon=True,
        ).start()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = threading.Thread(
            target=ClaudeProcess._pump_stderr,
            args=(self, process, self.stderr_lines),
            daemon=True,
        )
        self._stderr_thread.start()
        try:
            self._state()
        except Exception:
            self._close_process(kill=True)
            raise
        return process

    @staticmethod
    def _pump_pi_stdout(process, output_queue):
        try:
            for raw in process.stdout:
                output_queue.put(raw)
        finally:
            output_queue.put(None)

    def _read_event(self, timeout):
        with self.process_lock:
            output_queue = self._stdout_queue
        if output_queue is None:
            return None
        try:
            raw = output_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("pi emitted invalid JSON: %s" % exc) from exc

    def _send(self, command):
        with self._write_lock:
            with self.process_lock:
                process = self.process
            if process is None or process.poll() is not None:
                raise self._empty_stream_error(process)
            process.stdin.write(_json_line(command))
            process.stdin.flush()

    def _command(self, command, timeout=INIT_READ_TIMEOUT):
        self._send(command)
        while True:
            event = self._read_event(timeout)
            if event is None:
                raise self._empty_stream_error(self.process)
            if event.get("type") == "response" and event.get("command") == command.get(
                "type"
            ):
                if not event.get("success", False):
                    raise RuntimeError(
                        "%s failed: %s" % (command["type"], json.dumps(event)[:1500])
                    )
                return event.get("data") or {}

    def _session_path(self, session_id):
        sessions_dir = os.path.join(self._child_env()["PI_HOME"], "sessions")
        return os.path.join(sessions_dir, "%s.jsonl" % session_id)

    def _state(self):
        data = self._command({"type": "get_state"})
        session_file = data.get("sessionFile")
        session_id = data.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        if isinstance(session_file, str) and session_file:
            self._session_file = session_file
        model = data.get("model")
        if isinstance(model, dict):
            model_id = model.get("id")
            if isinstance(model_id, str):
                self._model = model_id
        return data

    def _empty_stream_error(self, process):
        code = None if process is None else process.poll()
        if code is None:
            if process is not None:
                code = process.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        error_msg = "pi exited before agent_end, exit code %s" % code
        stderr = _truncate_ring_for_error(self.stderr_lines)
        if stderr:
            error_msg += "\nCLI stderr:\n%s" % stderr
        return RuntimeError(error_msg)

    def _translate_activity_event(self, event):
        event_type = event.get("type")
        if event_type in ("tool_start", "tool_execution_start"):
            tool_name = event.get("toolName") or event.get("tool_name")
            args = event.get("args", event.get("input", {}))
            if tool_name:
                return {
                    "type": "tool_execution_start",
                    "toolName": str(tool_name).lower(),
                    "args": args,
                }
        return event

    def turn(
        self,
        message,
        session_id=None,
        model=DEFAULT_PI_MODEL,
        progress_token=None,
    ):
        with self.turn_lock:
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            with self.process_lock:
                process = self.process
            model_name = PI_MODELS.get(model, PI_MODELS[DEFAULT_PI_MODEL])
            if process is None or process.poll() is not None:
                self._close_process(kill=False)
                process = self._spawn(model)
            elif self._model != model_name:
                self._command(
                    {
                        "type": "set_model",
                        "provider": "openai-completions",
                        "modelId": model_name,
                    }
                )
                self._model = model_name
            requested_session = session_id or self.session_id
            if requested_session and requested_session != self.session_id:
                try:
                    data = self._command(
                        {
                            "type": "switch_session",
                            "sessionPath": self._session_path(requested_session),
                        }
                    )
                except RuntimeError as exc:
                    raise SessionConflictError(
                        "switch_session failed for session %s: %s"
                        % (requested_session, exc)
                    ) from exc
                if data.get("cancelled"):
                    raise SessionConflictError(
                        "switch_session cancelled for session %s" % requested_session
                    )
                self._state()
            result_text = ""
            usage = {}
            events = []
            accumulated_text = ""
            cached_activities = []
            activities_are_stale = True
            terminal_reason = "completed"
            self._turn_done.clear()
            self._in_flight = True
            try:
                pusher = _ProgressPusher(progress_token) if progress_token else None
            except Exception:
                pusher = None
            try:
                self._send({"type": "prompt", "message": message})
                while True:
                    try:
                        event = self._read_event(TURN_READ_TIMEOUT)
                    except TimeoutError as exc:
                        self._close_process(kill=True)
                        raise RuntimeError(
                            "timed out waiting for Pi output after %s seconds"
                            % TURN_READ_TIMEOUT
                        ) from exc
                    if event is None:
                        raise self._empty_stream_error(process)
                    if event.get("type") == "response":
                        if event.get("command") == "prompt" and not event.get(
                            "success"
                        ):
                            raise RuntimeError(
                                "prompt failed: %s" % json.dumps(event)[:1500]
                            )
                        continue
                    events.append(self._translate_activity_event(event))
                    if event.get("type") == "session":
                        candidate = event.get("id")
                        if isinstance(candidate, str) and candidate:
                            self.session_id = candidate
                    elif event.get("type") == "message_end":
                        message_event = event.get("message", {})
                        if message_event.get("role") == "assistant":
                            message_text = "".join(
                                item.get("text", "")
                                for item in message_event.get("content", [])
                                if item.get("type") == "text"
                            )
                            result_text = message_text
                            accumulated_text += message_text
                            raw_usage = message_event.get("usage", {})
                            usage = {
                                "input_tokens": raw_usage.get("input", 0),
                                "output_tokens": raw_usage.get("output", 0),
                                "cache_read_tokens": raw_usage.get("cacheRead", 0),
                                "cache_write_tokens": raw_usage.get("cacheWrite", 0),
                            }
                            terminal_reason = message_event.get(
                                "stopReason", "completed"
                            )
                            if pusher:
                                try:
                                    pusher.push(accumulated_text, cached_activities)
                                except Exception:
                                    pass
                    elif event.get("type") in (
                        "tool_start",
                        "tool_end",
                        "tool_execution_start",
                        "tool_execution_end",
                    ):
                        activities_are_stale = True
                    if activities_are_stale:
                        cached_activities = activity_from_events(events)[-300:]
                        activities_are_stale = False
                        if pusher:
                            try:
                                pusher.push(accumulated_text, cached_activities)
                            except Exception:
                                pass
                    if event.get("type") == "agent_end":
                        if not result_text:
                            for message_event in reversed(event.get("messages", [])):
                                if message_event.get("role") == "assistant":
                                    result_text = "".join(
                                        item.get("text", "")
                                        for item in message_event.get("content", [])
                                        if item.get("type") == "text"
                                    )
                                    break
                        if pusher:
                            try:
                                pusher.push(
                                    accumulated_text or result_text, cached_activities
                                )
                            except Exception:
                                pass
                        if not result_text:
                            error_detail = ""
                            for message_event in reversed(event.get("messages", [])):
                                if message_event.get("errorMessage"):
                                    error_detail = str(message_event["errorMessage"])
                                    break
                            if not error_detail:
                                error_detail = (
                                    "terminal event carried no text: %s"
                                    % (json.dumps(event)[:1500])
                                )
                            stderr = _truncate_ring_for_error(self.stderr_lines)
                            if stderr:
                                error_detail += "\nCLI stderr:\n%s" % stderr
                            raise RuntimeError(
                                "pi turn produced no output: %s" % error_detail
                            )
                        self._state()
                        record = {
                            "result": result_text,
                            "terminal_reason": terminal_reason,
                            "session_id": self.session_id,
                            "usage": usage,
                            "voice": voice_summary(result_text),
                            "activity": activity_from_events(events),
                        }
                        return record
            finally:
                if pusher:
                    try:
                        pusher.stop()
                    except Exception:
                        pass
                self._in_flight = False
                self._turn_done.set()

    def interrupt(self, timeout=INTERRUPT_TIMEOUT):
        with self.process_lock:
            process = self.process
        if process is None or process.poll() is not None or not self._in_flight:
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        self._in_flight = False
        try:
            self._send({"type": "abort"})
        except Exception:
            self._close_process(kill=True)
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        timed_out = not self._turn_done.wait(timeout=timeout)
        if timed_out:
            self._close_process(kill=True)
        return {
            "terminal_reason": "user_interrupt",
            "killed": timed_out,
            "timeout": timed_out,
        }

    def _close_process(self, kill=False):
        with self.process_lock:
            process = self.process
            self.process = None
            self._stdout_queue = None
            self._model = None
            self._in_flight = False
        self._turn_done.set()
        if process is None:
            return
        if kill and process.poll() is None:
            process.kill()
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _managed_child_pids.discard(process.pid)
        _reap_orphans()


def _sync_session_volume():
    """Flush guest writes to the durable session volume.

    Park tears the VM down with a hard SIGKILL and the drive runs in
    firecracker's Unsafe cache mode, so a turn's CLI session files sit in the
    guest page cache until the periodic ext4 commit and are lost on the kill.
    sync() after each turn pushes them to the device while the session is
    still live, so a later park -> rejoin finds the conversation intact
    (issue #4309).
    """
    try:
        os.sync()
    except OSError as exc:
        # A sync failure must never fail a turn: log and continue.
        sys.stderr.write("ember-claude-shim: session volume sync failed: %s\n" % exc)
        sys.stderr.flush()


class ProcessManager:
    """Route Claude-compatible turns to the selected CLI adapter."""

    def __init__(
        self,
        workspace=None,
        claude_executable="claude",
        codex_executable="codex",
        pi_executable="pi",
    ):
        _ensure_persistence_mountpoint_writable(_persistence_mount_path())
        self.workspace = os.path.abspath(
            workspace or os.environ.get("EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE)
        )
        self._hydration_attempts = 0
        self._hydration_error = None
        self._checkout_dir = None
        self._hydration_status = None
        _write_git_proxy_helper()
        self.claude = ClaudeProcess(self.workspace, claude_executable)
        self.codex = CodexProcess(self.workspace, codex_executable)
        self.pi = PiProcess(self.workspace, pi_executable)

    def _hydrate_workspace(self, repo, branch):
        if self._hydration_status == "ok":
            if self._checkout_dir and os.path.isdir(self._checkout_dir):
                self._hydration_status = "skipped_existing"
            return
        if self._hydration_status == "skipped_existing":
            return
        if self._hydration_attempts >= HYDRATION_ATTEMPT_CAP:
            return
        if self._hydration_attempts:
            sys.stderr.write(
                "ember-claude-shim: retrying workspace hydration for %s@%s "
                "(attempt %s/%s)\n"
                % (
                    repo,
                    branch,
                    self._hydration_attempts + 1,
                    HYDRATION_ATTEMPT_CAP,
                )
            )
            sys.stderr.flush()
        self._hydration_attempts += 1
        checkout_dir = os.path.join(self.workspace, "src")
        # Idempotency gate: restored/rejoined volumes reuse existing checkout.
        # This deliberately ignores repo/branch changes on restored volumes; per-session
        # volumes make that unreachable in practice (repo fixed at session create).
        if os.path.isdir(checkout_dir):
            try:
                validation = subprocess.run(
                    ["git", "-C", checkout_dir, "rev-parse", "--verify", "HEAD"],
                    capture_output=True,
                    timeout=5,
                    **_cli_privilege_kwargs(),
                )
                if validation.returncode == 0:
                    self._checkout_dir = checkout_dir
                    self._hydration_status = "skipped_existing"
                    # Resume slugs are safe: session cwd never changes mid-lineage (repo fixed at create).
                    self.claude.workspace = checkout_dir
                    self.codex.workspace = checkout_dir
                    self.pi.workspace = checkout_dir
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
            # A durable volume can contain a partial clone from a prior failure.
            shutil.rmtree(checkout_dir, ignore_errors=True)
        os.makedirs(self.workspace, exist_ok=True)
        clone_command = [
            "git",
            "clone",
            "--branch",
            branch,
            "--config",
            "core.gitProxy=%s" % GIT_PROXY_PATH,
            "--single-branch",
            "--filter=blob:none",
            "git://git-mirror.monolith.svc.cluster.local:9418/%s" % repo,
            checkout_dir,
        ]
        try:
            result = subprocess.run(
                clone_command,
                capture_output=True,
                timeout=GIT_CLONE_TIMEOUT_SECONDS,
                **_cli_privilege_kwargs(),
            )
            if result.returncode != 0:
                stderr = result.stderr
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", "replace")
                stderr = (stderr or "").strip()
                raise RuntimeError(
                    "git command failed with exit code %s%s"
                    % (result.returncode, ": " + stderr if stderr else "")
                )
            validation = subprocess.run(
                ["git", "-C", checkout_dir, "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                timeout=5,
                **_cli_privilege_kwargs(),
            )
            if validation.returncode != 0:
                raise RuntimeError("cloned directory validation failed: HEAD not found")
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(checkout_dir, ignore_errors=True)
            self._hydration_error = str(exc)
            sys.stderr.write(
                "ember-claude-shim: workspace hydration failed for %s@%s: %s\n"
                % (repo, branch, exc)
            )
            sys.stderr.flush()
            return
        except Exception as exc:
            shutil.rmtree(checkout_dir, ignore_errors=True)
            self._hydration_error = str(exc)
            sys.stderr.write(
                "ember-claude-shim: workspace hydration failed for %s@%s: %s\n"
                % (repo, branch, exc)
            )
            sys.stderr.flush()
            return
        self._hydration_error = None
        exclude_file = os.path.join(checkout_dir, ".git/info/exclude")
        os.makedirs(os.path.dirname(exclude_file), exist_ok=True)
        with open(exclude_file, "a") as stream:
            stream.write(".codex/\n.pi/\n")
        self._checkout_dir = checkout_dir
        self._hydration_status = "ok"
        self.claude.workspace = checkout_dir
        self.codex.workspace = checkout_dir
        self.pi.workspace = checkout_dir

    def _adapter(self, model):
        if model == "qwen":
            return self.pi
        if isinstance(model, str) and model in CODEX_MODELS:
            return self.codex
        return self.claude

    def ready(self):
        return self.claude.ready() and self.codex.ready() and self.pi.ready()

    def turn(
        self,
        message,
        session_id=None,
        model=None,
        repo=None,
        branch=None,
        progress_token=None,
    ):
        ensure_workspace_volume()
        if repo is not None and branch is not None:
            self._hydrate_workspace(repo, branch)
        adapter = self._adapter(model)
        try:
            extra = {"progress_token": progress_token} if progress_token else {}
            if adapter is self.pi:
                record = adapter.turn(
                    message, session_id, model or DEFAULT_PI_MODEL, **extra
                )
            elif adapter is self.codex:
                record = adapter.turn(
                    message, session_id, model or DEFAULT_CODEX_MODEL, **extra
                )
            else:
                record = adapter.turn(message, session_id, model, **extra)
            if repo is not None and isinstance(record, dict):
                if self._hydration_error:
                    record["workspace_hydration"] = {"failed": self._hydration_error}
                elif self._hydration_status:
                    record["workspace_hydration"] = self._hydration_status
            return record
        finally:
            # End-of-turn quiescence point: park only happens after a completed
            # turn plus idle, so flushing here guarantees the device has the
            # full session file well before any later park SIGKILLs the VM
            # (#4309). The finally covers every family, and both a normal
            # return AND a raised turn (a read timeout, a mid-turn CLI death):
            # the shim process stays alive through the exception, so the sync
            # is still safe, and a raised turn may still have left durable-
            # worth CLI state that a later park would otherwise lose.
            _sync_session_volume()

    def interrupt(self):
        # An interrupt has no model in its request, so interrupt both adapters.
        pi_result = self.pi.interrupt()
        codex_result = self.codex.interrupt()
        claude_result = self.claude.interrupt()
        if claude_result.get("killed"):
            return claude_result
        if codex_result.get("killed"):
            return codex_result
        return pi_result

    def _close_process(self, kill=False):
        self.claude._close_process(kill=kill)
        self.codex._close_process(kill=kill)
        self.pi._close_process(kill=kill)


Manager = ProcessManager


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
        repo = payload.get("repo")
        branch = payload.get("branch")
        if (repo is None) != (branch is None):
            self._send(400, {"error": "repo and branch must be provided together"})
            return
        if repo is not None and (
            not isinstance(repo, str)
            or not repo
            or not isinstance(branch, str)
            or not branch
        ):
            self._send(400, {"error": "repo and branch must be non-empty strings"})
            return
        progress_token = payload.get("progress_token")
        if progress_token is not None and (
            not isinstance(progress_token, str) or not progress_token.strip()
        ):
            self._send(400, {"error": "progress_token must be a non-empty string"})
            return
        try:
            hydration = {"repo": repo, "branch": branch} if repo is not None else {}
            progress = (
                {"progress_token": progress_token.strip()} if progress_token else {}
            )
            record = self.manager.turn(
                message,
                payload.get("session_id"),
                payload.get("model"),
                **(hydration | progress),
            )
            if repo is not None and isinstance(record, dict):
                if getattr(self.manager, "_hydration_error", None):
                    record["workspace_hydration"] = {
                        "failed": self.manager._hydration_error
                    }
                elif getattr(self.manager, "_hydration_status", None):
                    record["workspace_hydration"] = self.manager._hydration_status
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
    manager = ProcessManager(
        claude_executable="claude", codex_executable="codex", pi_executable="pi"
    )
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
