"""Private k8s-terminal demo: a k9s PTY over WebSocket against scratch-k8s.

The scratch-k8s workload is a scale-to-zero composite group (three Firecracker
microVMs forming a real k3s cluster) behind a wake-on-connect entry. This
module gives the private demos page a live terminal into it:

- ``GET  /api/demos/k8s/status``   the group's lifecycle state + wake history
  (the frontend's up/down band and state chip).
- ``WS   /api/demos/k8s/terminal`` wakes the cluster if it is banked (streaming
  phase events to the client while it boots), then spawns ``k9s`` in a PTY and
  bridges raw bytes both ways.

Wire protocol on the WebSocket: TEXT frames are JSON control messages
(server->client ``{"type": "phase" | "ready" | "error" | "exit", ...}``,
client->server ``{"type": "resize", "cols": N, "rows": N}``); BINARY frames are
raw PTY bytes in both directions once ``ready`` has been sent.

Single-session: one live PTY at a time, newest connection wins (the previous
socket gets an ``error`` control frame and closes). The demo cluster is a
singleton, k9s sessions are personal, and takeover beats a wedged tab.

Auth is the Cloudflare Access perimeter, like every other private route (this
domain has no ``register_public`` hook, so none of this exists in the public
binary). The kubeconfig token is the group's stable EMBER_GROUP_SECRET, read
from the embervm namespace via the Kubernetes API (RBAC: a resourceNames-scoped
Role in the embervm chart grants this pod's ServiceAccount ``get`` on exactly
that one Secret). k9s's own API-server traffic flows through the serving entry,
which conveniently keeps the group awake exactly as long as a terminal is open.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import tempfile
import termios
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from sqlmodel import Session

from app.db import get_engine
from ember_public.core import EMBERVM_URL
from shared.k8s_auth import auth_headers, service_account_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/demos/k8s", tags=["demos"])

# The composite workload this terminal fronts, and its serving entry (the
# wake-on-connect address k9s dials through). Overridable for tests/local.
_WORKLOAD = os.environ.get("EMBER_K8S_WORKLOAD", "scratch-k8s")
_ENTRY_HOST = os.environ.get(
    "EMBER_K8S_ENTRY_HOST", "embervm-embervm-serving.embervm.svc.cluster.local"
)
_ENTRY_PORT = int(os.environ.get("EMBER_K8S_ENTRY_PORT", "5410"))

# Where the group's kubeconfig bearer token lives (the stable per-group secret,
# survives banks/relights/rolls). Read via the Kubernetes API at session start.
_SECRET_NAMESPACE = os.environ.get("EMBER_K8S_SECRET_NAMESPACE", "embervm")
_SECRET_NAME = os.environ.get("EMBER_K8S_SECRET_NAME", "embervm-embervm-scratch-k8s")
_SECRET_KEY = "EMBER_GROUP_SECRET"

# Wake budget: the workload's wakeTimeoutSeconds is 180; poll a little past it
# so a slow relight-then-fresh-fallback still resolves rather than erroring.
_WAKE_TIMEOUT_S = 210
_POLL_INTERVAL_S = 1.0

# k9s process teardown grace before SIGKILL.
_TERM_GRACE_S = 3.0

_K9S_BIN = "/usr/local/bin/k9s"
_KUBECTL_BIN = "/usr/local/bin/kubectl"

# Light k9s skin matched to the private demos palette (paper background, ink
# text, slate accent). Written into K9S_CONFIG_DIR before each spawn so the
# terminal reads as part of the page rather than a dark hole in it.
_K9S_SKIN_LIGHT = """k9s:
  body:
    fgColor: "#0a0a0a"
    bgColor: "#ffffff"
    logoColor: "#1746a2"
  prompt:
    fgColor: "#0a0a0a"
    bgColor: "#ffffff"
    suggestColor: "#4a4a4a"
  info:
    fgColor: "#3a3a3a"
    sectionColor: "#0a0a0a"
  dialog:
    fgColor: "#0a0a0a"
    bgColor: "#ffffff"
    buttonFgColor: "#ffffff"
    buttonBgColor: "#1746a2"
    buttonFocusFgColor: "#ffffff"
    buttonFocusBgColor: "#b3261e"
    labelFgColor: "#7a5200"
    fieldFgColor: "#0a0a0a"
  frame:
    border:
      fgColor: "#9aa4b2"
      focusColor: "#1746a2"
    menu:
      fgColor: "#0a0a0a"
      keyColor: "#1746a2"
      numKeyColor: "#b3261e"
    crumbs:
      fgColor: "#ffffff"
      bgColor: "#1746a2"
      activeColor: "#7a5200"
    status:
      newColor: "#1746a2"
      modifyColor: "#7a5200"
      addColor: "#0f6d33"
      pendingColor: "#7a5200"
      errorColor: "#b3261e"
      highlightColor: "#1746a2"
      killColor: "#3a3a3a"
      completedColor: "#3a3a3a"
    title:
      fgColor: "#0a0a0a"
      bgColor: "#ffffff"
      highlightColor: "#1746a2"
      counterColor: "#7a5200"
      filterColor: "#0f6d33"
  views:
    charts:
      bgColor: "#ffffff"
      defaultDialColors:
        - "#1746a2"
        - "#b3261e"
      defaultChartColors:
        - "#1746a2"
        - "#b3261e"
    table:
      fgColor: "#0a0a0a"
      bgColor: "#ffffff"
      cursorFgColor: "#ffffff"
      cursorBgColor: "#1746a2"
      markColor: "#7a5200"
      header:
        fgColor: "#0a0a0a"
        bgColor: "#eef1f5"
        sorterColor: "#b3261e"
    xray:
      fgColor: "#0a0a0a"
      bgColor: "#ffffff"
      cursorColor: "#1746a2"
      graphicColor: "#1746a2"
      showIcons: false
    yaml:
      keyColor: "#1746a2"
      colonColor: "#3a3a3a"
      valueColor: "#0a0a0a"
    logs:
      fgColor: "#0a0a0a"
      bgColor: "#ffffff"
      indicator:
        fgColor: "#ffffff"
        bgColor: "#1746a2"
"""

# Minimal k9s config selecting the skin; k9s fills in the rest of its schema
# with defaults and rewrites the file freely (the dir is per-session tmpfs-ish).
_K9S_CONFIG = """k9s:
  ui:
    skin: light
    logoless: true
  logger:
    tail: 200
"""


def _write_k9s_config() -> None:
    cfg_dir = "/tmp/k8s-demo-home/k9s"
    os.makedirs(os.path.join(cfg_dir, "skins"), exist_ok=True)
    with open(os.path.join(cfg_dir, "skins", "light.yaml"), "w", encoding="utf-8") as f:
        f.write(_K9S_SKIN_LIGHT)
    cfg_path = os.path.join(cfg_dir, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(_K9S_CONFIG)


# The single live session (newest connection wins). Guarded by _session_lock.
_session_lock = asyncio.Lock()
_current_session: "_TerminalSession | None" = None


# -- control-plane status ----------------------------------------------------


async def fetch_group_status() -> dict:
    """GET the control plane's composite introspection for the workload."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{EMBERVM_URL}/v1/groups/{_WORKLOAD}",
            headers=auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _shape_status(raw: dict) -> dict:
    """The frontend's view of the group: state + warmth + member summary."""
    instance = raw.get("instance") or {}
    return {
        "workload": raw.get("workload", _WORKLOAD),
        "state": raw.get("state"),
        # A non-null set_id means a complete banked snapshot set exists: the
        # next wake ATTEMPTS a warm relight (falls back to fresh on failure).
        "warm": bool(raw.get("set_id")),
        "created_at": instance.get("created_at"),
        "last_active_at": instance.get("last_active_at"),
        "members": [
            {
                "name": m.get("member_name"),
                "state": m.get("state"),
                "healthy": m.get("healthy"),
            }
            for m in raw.get("members", [])
        ],
    }


def _recent_wake_events(limit: int = 50) -> list[dict]:
    """The wake history rows the frontend renders as the up/down band.

    Best-effort: a missing table (migration not yet applied on this rollout)
    degrades to an empty band rather than failing the status endpoint.
    """
    try:
        with Session(get_engine()) as session:
            rows = session.exec(
                text(
                    """
                    SELECT at, from_state, duration_ms, classification
                    FROM ember_k8s_wake_event
                    ORDER BY at DESC
                    LIMIT :limit
                    """
                ).bindparams(limit=limit)
            ).all()
    except Exception:  # noqa: BLE001
        logger.exception("k8s demo: wake-event history unavailable")
        return []
    return [
        {
            "at": row[0].isoformat() if row[0] else None,
            "from_state": row[1],
            "duration_ms": row[2],
            "classification": row[3],
        }
        for row in rows
    ]


def _record_wake_event(from_state: str, duration_ms: int, classification: str) -> None:
    try:
        with Session(get_engine()) as session:
            session.exec(
                text(
                    """
                    INSERT INTO ember_k8s_wake_event
                        (at, from_state, duration_ms, classification)
                    VALUES (:at, :from_state, :duration_ms, :classification)
                    """
                ).bindparams(
                    at=datetime.now(UTC),
                    from_state=from_state,
                    duration_ms=duration_ms,
                    classification=classification,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - history is best-effort, never fails a wake
        logger.exception("k8s demo: recording wake event failed")


@router.get("/status")
async def k8s_status() -> dict:
    raw = await fetch_group_status()
    shaped = _shape_status(raw)
    shaped["events"] = _recent_wake_events()
    shaped["session_active"] = _current_session is not None
    return shaped


# -- kubeconfig ---------------------------------------------------------------


async def _read_group_secret() -> str:
    """Read the group's EMBER_GROUP_SECRET via the Kubernetes API.

    A plain REST call (kubernetes.default.svc with the pod CA + SA token)
    rather than the kubernetes_asyncio client: this is one GET of one named
    Secret, and the debug client in ``cluster/`` is scoped to cluster reads.
    """
    token = service_account_token()
    if not token:
        raise RuntimeError("no ServiceAccount token (off-cluster?)")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    url = (
        f"https://{host}:{port}/api/v1/namespaces/{_SECRET_NAMESPACE}"
        f"/secrets/{_SECRET_NAME}"
    )
    async with httpx.AsyncClient(timeout=10, verify=ca) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        data = resp.json().get("data", {})
    encoded = data.get(_SECRET_KEY)
    if not encoded:
        raise RuntimeError(f"secret {_SECRET_NAME} has no {_SECRET_KEY} key")
    return base64.b64decode(encoded).decode()


def _write_kubeconfig(token: str) -> str:
    """Write a kubeconfig for the group entry; returns its path.

    insecure-skip-tls-verify because the inner k3s serves a self-signed cert
    for its own SANs; the transport to the entry is in-cluster. The token is
    the group secret (k3s token-auth file entry, decision 13).
    """
    fd, path = tempfile.mkstemp(prefix="k8s-demo-kubeconfig-")
    os.close(fd)
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "scratch-k8s",
                "cluster": {
                    "server": f"https://{_ENTRY_HOST}:{_ENTRY_PORT}",
                    "insecure-skip-tls-verify": True,
                },
            }
        ],
        "users": [{"name": "demo", "user": {"token": token}}],
        "contexts": [
            {
                "name": "scratch-k8s",
                "context": {"cluster": "scratch-k8s", "user": "demo"},
            }
        ],
        "current-context": "scratch-k8s",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    os.chmod(path, 0o600)
    return path


# -- wake ---------------------------------------------------------------------


async def _fire_wake_probe() -> None:
    """One connection to the entry to trigger wake-on-connect, then hang up.

    The connection parks behind the wake (or is reset when the activator errs);
    either way it has done its job the moment it reaches the entry. ONE probe,
    never a loop: repeated connects trip the parked-connection cap.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(_ENTRY_HOST, _ENTRY_PORT), timeout=5
        )
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    except Exception:  # noqa: BLE001 - a reset IS the expected cold-path outcome
        pass


async def _wake_and_wait(send_phase) -> tuple[str, int, str]:
    """Drive the group to running; returns (from_state, duration_ms, classification).

    ``send_phase`` is an async callable receiving progress dicts for the client.
    """
    raw = await fetch_group_status()
    from_state = raw.get("state") or "unknown"
    warm = bool(raw.get("set_id"))

    if from_state == "running":
        await send_phase({"state": "running", "note": "cluster already live"})
        return from_state, 0, "already-live"

    await send_phase(
        {
            "state": from_state,
            "note": "cluster is cold - firing wake-on-connect"
            + (" (banked snapshot set present, relight expected)" if warm else ""),
        }
    )
    started = time.monotonic()
    await _fire_wake_probe()

    last_state = from_state
    while time.monotonic() - started < _WAKE_TIMEOUT_S:
        await asyncio.sleep(_POLL_INTERVAL_S)
        try:
            raw = await fetch_group_status()
        except Exception:  # noqa: BLE001 - transient CP blips mid-wake are fine
            continue
        state = raw.get("state") or "unknown"
        if state != last_state:
            await send_phase({"state": state})
            last_state = state
        if state == "running":
            duration_ms = int((time.monotonic() - started) * 1000)
            classification = await _classify_wake(warm, duration_ms)
            return from_state, duration_ms, classification

    raise TimeoutError(f"group did not reach running within {_WAKE_TIMEOUT_S}s")


async def _classify_wake(warm_expected: bool, duration_ms: int) -> str:
    """Label the wake honestly: relit vs cold, judged from inner-node age.

    The CP status does not expose whether a wake relit or fell back to fresh,
    but the inner cluster does: a relit node's creationTimestamp predates the
    wake, a fresh-booted one is younger than it. Best-effort - classification
    never fails the session.
    """
    if not warm_expected:
        return "cold-boot"
    kubeconfig = None
    try:
        token = await _read_group_secret()
        kubeconfig = _write_kubeconfig(token)
        proc = await asyncio.create_subprocess_exec(
            _KUBECTL_BIN,
            "--kubeconfig",
            kubeconfig,
            "get",
            "nodes",
            "-o",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        nodes = json.loads(out)
        oldest_age_s = 0.0
        now = datetime.now(UTC)
        for item in nodes.get("items", []):
            ts = item["metadata"]["creationTimestamp"]
            created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            oldest_age_s = max(oldest_age_s, (now - created).total_seconds())
        # Nodes older than the wake itself (plus margin) survived the bank.
        if oldest_age_s > duration_ms / 1000 + 60:
            return "relit"
        return "cold-boot (relight fell back)"
    except Exception:  # noqa: BLE001
        logger.exception("k8s demo: wake classification failed")
        return "unclassified"
    finally:
        if kubeconfig:
            with contextlib.suppress(OSError):
                os.unlink(kubeconfig)


# -- the PTY session ----------------------------------------------------------


class _TerminalSession:
    """One live k9s PTY bridged to one WebSocket."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.master_fd: int | None = None
        self.child_pid: int | None = None
        self.exit_code: int | None = None
        self.closed = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def spawn(self, kubeconfig: str, cols: int, rows: int) -> None:
        # openpty here only to fail fast on fd exhaustion before forking; the
        # real pair comes from pty.fork below.
        master_fd, slave_fd = pty.openpty()
        env = {
            "KUBECONFIG": kubeconfig,
            "TERM": "xterm-256color",
            "HOME": "/tmp/k8s-demo-home",
            "K9S_CONFIG_DIR": "/tmp/k8s-demo-home/k9s",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "COLORTERM": "truecolor",
            # k9s is a static (cgo-free) binary: user.Current() fails without
            # $USER and k9s exits before drawing anything ("Fail to init k9s
            # logs location"). LOGNAME for the same lookup on other paths.
            "USER": "demo",
            "LOGNAME": "demo",
            # k9s logs land somewhere findable for the next debugging session.
            "XDG_STATE_HOME": "/tmp/k8s-demo-home/.state",
        }
        _write_k9s_config()
        os.close(slave_fd)
        os.close(master_fd)

        # pty.fork, not asyncio subprocess: k9s/tcell opens /dev/tty, which
        # needs a CONTROLLING terminal. start_new_session gives the child a
        # session with none (exit 1, "open /dev/tty: no such device"), and a
        # preexec_fn doing setsid+TIOCSCTTY raised under the threaded server.
        # pty.fork is built for exactly this: the child is a session leader
        # with the slave as its controlling tty, and the parent holds the
        # master. The child execs immediately (os._exit on any failure - no
        # interpreter cleanup may run in a forked child).
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                os.execve(_K9S_BIN, [_K9S_BIN, "--logoless"], env)
            finally:
                os._exit(127)
        self.child_pid = pid
        self.master_fd = master_fd
        self._resize(cols, rows)

    def _resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        cols = max(20, min(500, int(cols)))
        rows = max(5, min(200, int(rows)))
        winsz = struct.pack("HHHH", rows, cols, 0, 0)
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsz)

    async def pump(self) -> None:
        """Bridge PTY <-> WebSocket until either side closes."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        out_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        # The fd is captured as a LOCAL: a concurrent close() (session takeover)
        # nulls self.master_fd, and an armed reader firing in that window must
        # hit a plain closed-fd OSError (handled), never os.read(None).
        fd = self.master_fd

        def _on_readable() -> None:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if data:
                with contextlib.suppress(asyncio.QueueFull):
                    out_queue.put_nowait(data)
            else:
                # EOF: k9s exited (or the PTY died / was closed under us).
                loop.remove_reader(fd)
                with contextlib.suppress(asyncio.QueueFull):
                    out_queue.put_nowait(None)

        loop.add_reader(fd, _on_readable)

        async def to_client() -> None:
            while True:
                data = await out_queue.get()
                if data is None:
                    break
                await self.websocket.send_bytes(data)

        async def from_client() -> None:
            while True:
                message = await self.websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if (data := message.get("bytes")) is not None:
                    try:
                        os.write(fd, data)
                    except OSError:
                        break
                elif (textmsg := message.get("text")) is not None:
                    with contextlib.suppress(json.JSONDecodeError, KeyError):
                        control = json.loads(textmsg)
                        if control.get("type") == "resize":
                            self._resize(control["cols"], control["rows"])

        writer = asyncio.create_task(to_client())
        reader = asyncio.create_task(from_client())
        try:
            done, pending = await asyncio.wait(
                {writer, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                # Surface a pump crash (JSON error frames etc.) to the log.
                if task.exception() and not isinstance(
                    task.exception(), (WebSocketDisconnect, RuntimeError)
                ):
                    logger.warning("k8s demo pump error: %r", task.exception())
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)

    async def _reap(self, timeout: float) -> int | None:
        """waitpid the child (bounded); records + returns the exit code, or None."""
        if self.child_pid is None:
            return self.exit_code
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pid, status = os.waitpid(self.child_pid, os.WNOHANG)
            except ChildProcessError:
                return self.exit_code
            if pid == self.child_pid:
                self.exit_code = os.waitstatus_to_exitcode(status)
                return self.exit_code
            await asyncio.sleep(0.1)
        return None

    async def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        if self.child_pid is not None and self.exit_code is None:
            # pty.fork made the child a session leader, so pgid == pid.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.child_pid, signal.SIGTERM)
            if await self._reap(timeout=_TERM_GRACE_S) is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.child_pid, signal.SIGKILL)
                await self._reap(timeout=_TERM_GRACE_S)
        if self.master_fd is not None:
            # Disarm the reader BEFORE closing: close() runs concurrently with
            # the (old) session's pump during a takeover, and a reader firing
            # between os.close and pump's own remove_reader would spin on a
            # recycled fd number.
            if getattr(self, "_loop", None) is not None:
                with contextlib.suppress(Exception):
                    self._loop.remove_reader(self.master_fd)
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = None


@router.websocket("/terminal")
async def k8s_terminal(websocket: WebSocket) -> None:
    global _current_session
    await websocket.accept()

    # Newest connection wins: close any live session before starting ours.
    async with _session_lock:
        if _current_session is not None:
            with contextlib.suppress(Exception):
                await _current_session.websocket.send_text(
                    json.dumps({"type": "error", "error": "session taken over"})
                )
                await _current_session.websocket.close()
            await _current_session.close()
        session = _TerminalSession(websocket)
        _current_session = session

    async def send_phase(payload: dict) -> None:
        await websocket.send_text(json.dumps({"type": "phase", **payload}))

    try:
        # 1. Wake (streams phases while the microVMs boot/relight).
        from_state, duration_ms, classification = await _wake_and_wait(send_phase)
        if duration_ms:
            _record_wake_event(from_state, duration_ms, classification)
            await send_phase(
                {
                    "state": "running",
                    "note": f"{classification} in {duration_ms / 1000:.1f}s",
                    "duration_ms": duration_ms,
                    "classification": classification,
                }
            )

        # 2. Credentials + PTY.
        token = await _read_group_secret()
        kubeconfig = _write_kubeconfig(token)
        session.kubeconfig = kubeconfig
        cols = int(websocket.query_params.get("cols", 120))
        rows = int(websocket.query_params.get("rows", 32))
        await session.spawn(kubeconfig, cols, rows)
        await websocket.send_text(json.dumps({"type": "ready"}))

        # 3. Bridge until either side hangs up.
        await session.pump()
        await session._reap(timeout=1.0)
        exit_code = session.exit_code
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"type": "exit", "code": exit_code}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - report, then close
        logger.exception("k8s demo terminal session failed")
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"type": "error", "error": str(exc)}))
    finally:
        await session.close()
        if (kc := getattr(session, "kubeconfig", None)) is not None:
            with contextlib.suppress(OSError):
                os.unlink(kc)
        async with _session_lock:
            if _current_session is session:
                _current_session = None
        with contextlib.suppress(Exception):
            await websocket.close()
