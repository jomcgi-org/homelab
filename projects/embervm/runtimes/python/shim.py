"""EmberVM python runtime bootstrap shim (ADR embervm/001, R1 zip lane).

This shim runs inside a disposable python-runtime microVM, launched by the guest
init (a raw Firecracker boot ignores the OCI entrypoint, so Task 7 wires a PID-1
harnessInit that mounts a tmpfs over /tmp then execs this shim, see README "Boot
integration"). An adopter of the zip lane brings only a zip archive containing a
`handler(event, context)` callable.

The archive is NOT attached as a block device (that design truncated the archive
tail and pinned the snapshot to a node-local backing file). Instead noded fetches
and sha256-verifies the archive into memory host-side, boots this guest with NO
archive drive, and POSTs the clean bytes to the build-only /shim/hydrate endpoint
over the existing vsock HTTP channel (port 1027). The shim unpacks those bytes to
tmpfs, imports the handler, and only THEN flips /shim/ready to 200. The snapshot
noded then takes is memory + rootfs only: self-contained and portable (shippable
to S3/OCI, restorable on any node), because there is no archive backing file for
the restore to depend on.

The build/boot flow, entirely guest-side:

  1. On boot the shim starts its vsock HTTP server immediately and serves, but is
     NOT ready and has no handler. State: hydrated=False, ready=False.
  2. A build-only POST /shim/hydrate delivers the raw zip bytes (clean, no
     padding). The shim unpacks them into a tmpfs workdir. The host NEVER unpacks:
     the archive crosses the boundary as opaque bytes and is expanded only here,
     inside the throwaway guest.
  3. If the archive root holds an executable file named `bootstrap`, execs it
     instead of importing python (the any-language escape hatch). That process is
     then responsible for serving the same frozen guest contract.
  4. Otherwise imports the configured handler symbol (env EMBER_HANDLER, default
     `app.handle`). On success ready flips True; the shim then serves the frozen
     guest contract over HTTP on vsock port 1027 (embervm noded's GuestHTTPPort):
       - GET  /shim/healthz  -> 200 always (liveness; noded's Prime probe).
       - POST /shim/hydrate  -> build-only; unpack + import, flip ready on success
                                (200) or report the error (4xx/5xx) leaving ready
                                False. See below for idempotency.
       - GET  /shim/ready    -> 200 ONLY after a successful hydrate+import, 503
                                otherwise. noded's BuildBase health-gates on this,
                                so a hydrate failure keeps the base VM from ever
                                going ready and the error surfaces later in the
                                Workload condition.
       - POST <invokePath>   -> marshal the HTTP request into an event dict, call
                                handler(event, context), marshal the return into
                                the HTTP response. 503 before the shim is ready.

Event/response shape (normative, Lambda-compatible-enough):
  request  event    = {httpMethod, path, queryStringParameters, headers,
                       body (base64 if binary), isBase64Encoded}
  handler  return   = {statusCode, headers, body, isBase64Encoded}

Restore-safe contract: this shim is baked into a warm base that is snapshotted
once and restored per invoke. It caches NO wall-clock and seeds NO RNG at boot,
and adopters MUST NOT either: a function that reads time.time() or os.urandom()
once at import and reuses it will see the same frozen value across every
restored invoke. Read entropy and the clock per invocation, inside handler().

The shim is stdlib-only on purpose (http.server, zipfile) so it adds no
dependency to the baked runtime subset and can be unit-tested as plain python.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import os
import socket
import stat
import sys
import threading
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

# VMADDR_CID_ANY is the vsock wildcard local CID (bind on any). Python exposes
# socket.VMADDR_CID_ANY on Linux builds with vsock support, but the CI test
# runner may lack the vsock module entirely, so fall back to the kernel
# constant (0xffffffff) rather than import-failing the whole shim.
VMADDR_CID_ANY = getattr(socket, "VMADDR_CID_ANY", 0xFFFFFFFF)

# The frozen guest HTTP port. This MUST equal embervm noded's
# vsockproto.GuestHTTPPort (projects/embervm/noded/vsockproto/proto.go); the
# host dials it through the Firecracker CONNECT handshake. Do not change it
# without changing the contract on both sides.
GUEST_HTTP_PORT = 1027

# R3 serving mode (D-R3.11.1). Task/session guests answer over vsock (above);
# a SERVING guest answers over its tap NIC (plain TCP), because the R3 serving
# lane health-probes and proxies at the guest's real L3 endpoint
# (noded/server/serving.go: GET http://ip:port{healthPath}). When
# EMBER_SERVING_PORT is set and > 0 the shim binds AF_INET on 0.0.0.0:<port>
# using the SAME request handler as the vsock path and does NOT bind vsock (a
# serving VM has no vsock HTTP consumer). When it is unset the boot is the
# unchanged vsock path, so task/session guests are byte-identical. guest-init
# translates the `ember.serving_port=` kernel boot-arg (set by noded's
# bootArgsFor only on a serving cold boot) into this env var.
SERVING_PORT_ENV = "EMBER_SERVING_PORT"

# R3 zip-lane serving cold-boot handler import (D-R3.11.2). A serving VM must COLD
# BOOT with a NIC (Firecracker cannot hot-attach a NIC to a resumed snapshot), so it
# cannot resume the vsock-only base memory snapshot the zip lane bakes the handler
# into. Instead noded attaches the handler artifact (the verified zip bytes) as a
# SECOND read-only drive and sets EMBER_HANDLER_ZIP to that block device; the shim
# reads the zip off it and imports the handler BEFORE binding the serving socket, so
# the guest is ready at its first health probe without a runtime hydrate. This is the
# cold-boot analogue of the build-time POST /shim/hydrate. EMBER_HANDLER_ZIP_BYTES is
# the EXACT zip length: a block device is sector-padded, and Python's zipfile scans
# backward from the end for the EOCD signature, which trailing zero padding breaks, so
# the shim reads ONLY this many bytes off the device (the retired R1 sector-pad/EOCD
# bug class, defused by conveying the exact length instead of guessing where the zip
# ends). Unset (task/session boots, relights) leaves this path inert.
HANDLER_ZIP_ENV = "EMBER_HANDLER_ZIP"
HANDLER_ZIP_BYTES_ENV = "EMBER_HANDLER_ZIP_BYTES"

# Env-configured knobs, all read at boot with sane defaults (documented in
# README.md). EMBER_INVOKE_PATH defaults to /invoke, matching noded's
# server.defaultInvokePath, NOT "/" (a request that carries no path falls back
# to /invoke on the host side, so the shim must listen there by default).
DEFAULT_HANDLER = "app.handle"
DEFAULT_INVOKE_PATH = "/invoke"

# Where the archive is unpacked. A tmpfs mount is expected over /tmp (the rootfs
# is read-only and shared across restores); the workdir lives under it so the
# unpacked code is writable and per-guest.
UNPACK_DIR = "/tmp/ember-app"

READY_PATH = "/shim/ready"
HEALTHZ_PATH = "/shim/healthz"
HYDRATE_PATH = "/shim/hydrate"


class InvocationContext:
    """The `context` argument passed to handler(event, context).

    Deliberately minimal. Adopters that need request metadata read it from the
    event; this object only exposes the invoke path so a handler can sub-route,
    and stays open for extension without breaking the frozen shape.
    """

    def __init__(self, invoke_path: str) -> None:
        self.invoke_path = invoke_path


class ShimState:
    """Mutable guest-side state, shared by the request handler across threads.

    The shim boots un-hydrated (no handler, not ready) and serves anyway, so
    noded's Prime liveness probe answers immediately. A build-only POST to
    /shim/hydrate populates `handler` and flips `ready` True under the lock;
    /shim/ready and the invoke path read `ready` to gate.
    """

    def __init__(self, invoke_path: str, handler_symbol: str) -> None:
        self.invoke_path = invoke_path
        self.handler_symbol = handler_symbol
        self._lock = threading.Lock()
        self.handler: Callable[[Any, Any], Any] | None = None
        self.hydrated = False
        self.ready = False

    def is_ready(self) -> bool:
        with self._lock:
            return self.ready


def _is_within(base: str, target: str) -> bool:
    """True iff target resolves inside base (zip-slip guard).

    Uses realpath on both so a "../" entry or an absolute member path that
    escapes base is rejected. base itself is treated as inside.
    """
    base_real = os.path.realpath(base)
    target_real = os.path.realpath(target)
    return target_real == base_real or target_real.startswith(base_real + os.sep)


def unpack_archive(archive: bytes | io.BytesIO, dest: str) -> str:
    """Unpack the raw zip bytes into dest and return dest.

    `archive` is the clean, padding-free zip bytes (or a BytesIO over them) that
    noded delivered over /shim/hydrate; there is no block device and no trailing
    padding to trim. Rejects zip-slip: any member whose destination escapes dest
    (via "../" or an absolute path) aborts the whole unpack with a ValueError.
    This runs guest-side only; the host never sees the expanded tree.
    """
    if isinstance(archive, io.BytesIO):
        buf = archive
    else:
        buf = io.BytesIO(archive)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(buf) as zf:
        for member in zf.namelist():
            # Directory entries end in "/"; skip explicit creation, the file
            # writes below make parents as needed.
            target = os.path.join(dest, member)
            if not _is_within(dest, target):
                raise ValueError(
                    f"archive member escapes unpack dir (zip-slip rejected): {member!r}"
                )
        zf.extractall(dest)
    return dest


def _find_bootstrap(app_dir: str) -> str | None:
    """Return the path to an executable `bootstrap` in app_dir root, or None.

    The escape hatch: an archive whose root contains an executable file named
    `bootstrap` runs that instead of the python handler path, letting any
    language serve the contract.
    """
    candidate = os.path.join(app_dir, "bootstrap")
    if not os.path.isfile(candidate):
        return None
    mode = os.stat(candidate).st_mode
    if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return candidate
    return None


def load_handler(app_dir: str, handler_symbol: str) -> Callable[[Any, Any], Any]:
    """Import handler_symbol ("module.attr") from code under app_dir.

    app_dir is prepended to sys.path so the adopter's top-level modules import.
    Raises on a missing module, a missing attribute, or a non-callable target;
    the caller turns any raise into a failed hydrate (ready never flips).
    """
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    module_name, sep, attr = handler_symbol.rpartition(".")
    if not sep:
        raise ValueError(f"EMBER_HANDLER must be 'module.attr', got {handler_symbol!r}")

    module = importlib.import_module(module_name)
    handler = getattr(module, attr, None)
    if handler is None:
        raise AttributeError(f"handler {attr!r} not found in module {module_name!r}")
    if not callable(handler):
        raise TypeError(f"handler {handler_symbol!r} is not callable")
    return handler


def hydrate(state: ShimState, archive: bytes) -> None:
    """Unpack the archive bytes, run the bootstrap check, import the handler.

    On success sets state.handler, state.hydrated, and state.ready True. On any
    failure (bad zip, zip-slip, import error) raises with state.ready left False;
    the caller (do_POST) turns the raise into a non-2xx hydrate response and
    writes the traceback to the console.

    A `bootstrap` executable in the archive root replaces this process via execv
    (the any-language escape hatch): the call never returns, so the shim is
    handed wholesale to bootstrap, which owns serving the contract.
    """
    app_dir = unpack_archive(archive, UNPACK_DIR)
    bootstrap = _find_bootstrap(app_dir)
    if bootstrap is not None:
        # Any-language escape hatch: hand the whole guest over to bootstrap. It
        # never returns; it must serve the contract itself (including readiness).
        sys.stderr.write(f"ember-shim: exec bootstrap {bootstrap}\n")
        sys.stderr.flush()
        exec_bootstrap(bootstrap)
        return  # unreachable; execv replaced the process
    handler = load_handler(app_dir, state.handler_symbol)
    with state._lock:  # noqa: SLF001 (state's own module)
        state.handler = handler
        state.hydrated = True
        state.ready = True


def event_from_request(
    method: str, path: str, query: str, headers: dict[str, str], raw_body: bytes
) -> dict[str, Any]:
    """Marshal an inbound HTTP request into the normative event dict.

    The body is UTF-8 decoded when it is valid text; a body that is not valid
    UTF-8 is base64-encoded with isBase64Encoded=true so binary triggers (an
    image, a protobuf) survive the JSON round-trip intact.
    """
    query_params = _parse_query(query)
    try:
        body_str: str | None = raw_body.decode("utf-8")
        is_b64 = False
    except UnicodeDecodeError:
        body_str = base64.b64encode(raw_body).decode("ascii")
        is_b64 = True
    if not raw_body:
        # An empty body is represented as None, not "" or a base64 blank, so a
        # handler can cleanly distinguish "no body".
        body_str = None
        is_b64 = False

    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": query_params,
        "headers": headers,
        "body": body_str,
        "isBase64Encoded": is_b64,
    }


def _parse_query(query: str) -> dict[str, str] | None:
    if not query:
        return None
    from urllib.parse import parse_qsl

    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return None
    # Last-value-wins on repeats, matching the single-value queryStringParameters
    # shape (a multi-value shape is a separate field adopters do not get in v1).
    return {k: v for k, v in pairs}


def response_from_return(result: Any) -> tuple[int, dict[str, str], bytes]:
    """Marshal a handler return value into (status, headers, body bytes).

    Accepts the normative dict {statusCode, headers, body, isBase64Encoded}.
    A bare string or bytes return is treated as a 200 text/binary body for
    ergonomics. A dict body that is base64-flagged is decoded back to bytes; a
    dict/list body is JSON-encoded. A missing statusCode defaults to 200.
    """
    if isinstance(result, dict) and (
        "statusCode" in result or "body" in result or "headers" in result
    ):
        status = int(result.get("statusCode", 200))
        headers = {str(k): str(v) for k, v in (result.get("headers") or {}).items()}
        body = result.get("body")
        is_b64 = bool(result.get("isBase64Encoded", False))
        return status, headers, _encode_body(body, is_b64)

    # Ergonomic shortcuts for handlers that just return a payload.
    if isinstance(result, (bytes, bytearray)):
        return 200, {}, bytes(result)
    if isinstance(result, str):
        return 200, {}, result.encode("utf-8")
    # Any other JSON-serializable return: encode as JSON with a content type.
    return 200, {"Content-Type": "application/json"}, json.dumps(result).encode("utf-8")


def _encode_body(body: Any, is_b64: bool) -> bytes:
    if body is None:
        return b""
    if is_b64:
        if not isinstance(body, str):
            raise TypeError("isBase64Encoded response body must be a str")
        return base64.b64decode(body)
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    # A structured body without isBase64Encoded is JSON-encoded.
    return json.dumps(body).encode("utf-8")


def make_request_handler(
    state: ShimState,
    invoke_path: str,
    *,
    serving: bool = False,
) -> type[BaseHTTPRequestHandler]:
    """Build the BaseHTTPRequestHandler class serving the frozen contract.

    All handler/ready state lives in `state`, mutated by a successful hydrate.
    Before hydration /shim/ready reports 503 and any POST to the invoke path is
    503; /shim/healthz is 200 throughout so noded's Prime probe answers.

    Two dispatch modes share this handler:

    - Task/session (vsock, ``serving=False``): the guest is an RPC endpoint. The
      handler is invoked ONLY on ``POST`` to ``invoke_path``; every other
      method/path is 404. This is byte-unchanged from the frozen contract.
    - Serving (TCP, ``serving=True``): the guest is a WEB SERVER, so it owns its
      own routing. Every request whose path is not one of the reserved ``/shim/*``
      contract paths is passed to the handler with its real method/path/query, so a
      browser ``GET /...?title=...`` (an OG-image card, say) reaches the handler.
      The reserved ``/shim/*`` namespace stays owned by the shim (health, ready,
      hydrate); it is never routed to the handler.
    """

    class ShimHTTPRequestHandler(BaseHTTPRequestHandler):
        # Quiet the default stderr access log; guest logs are shipped and this
        # per-request line is noise. Real errors are written explicitly below.
        def log_message(self, *_args: Any) -> None:  # noqa: D401
            pass

        def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            # Explicit Content-Length so the response is fixed-length framed and
            # not chunked (noded's vsock transport rejects chunked framing).
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            # A HEAD response carries the headers (Content-Length included) but no
            # body, per RFC 7231; serving mode invokes the handler to compute those
            # headers, then suppresses the body here.
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            path = self.path.split("?", 1)[0]
            if path == HEALTHZ_PATH:
                self._send(200, {}, b"")
                return
            if path == READY_PATH:
                if state.is_ready():
                    self._send(200, {}, b"")
                else:
                    self._send(503, {}, b"shim not ready: not hydrated")
                return
            # Serving mode: the guest is a web server, so a GET to any non-reserved
            # path invokes the handler (a browser fetching an OG-image card, say).
            # Task/session mode never serves a handler over GET: 404.
            if serving:
                self._serving_invoke()
                return
            self._send(404, {}, b"not found")

        def do_POST(self) -> None:  # noqa: N802 (http.server naming)
            path = self.path.split("?", 1)[0]
            if path == HYDRATE_PATH:
                self._handle_hydrate()
                return
            # Serving mode routes any non-reserved path to the handler; task/session
            # mode invokes ONLY the exact invoke_path (the frozen RPC contract).
            if serving:
                self._serving_invoke()
                return
            if path != invoke_path:
                self._send(404, {}, b"not found")
                return
            self._invoke_handler()

        # Serving mode: the web-app handler owns its routes and may use any verb.
        # Each maps to the same handler dispatch; the reserved /shim/* namespace is
        # guarded inside _serving_invoke. GET/POST are handled above (they also carry
        # the task/session contract); these cover the rest of a REST surface.
        def do_PUT(self) -> None:  # noqa: N802
            self._serving_only()

        def do_DELETE(self) -> None:  # noqa: N802
            self._serving_only()

        def do_PATCH(self) -> None:  # noqa: N802
            self._serving_only()

        def do_HEAD(self) -> None:  # noqa: N802
            self._serving_only()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._serving_only()

        def _serving_only(self) -> None:
            # A non-GET/POST verb only makes sense in serving mode; task/session mode
            # answers 405 (the RPC contract is POST-to-invoke_path only).
            if serving:
                self._serving_invoke()
            else:
                self._send(405, {}, b"method not allowed")

        def _serving_invoke(self) -> None:
            # The /shim/* namespace is the shim's contract surface (health, ready,
            # hydrate), never the app's, so it is never routed to the handler even in
            # serving mode: a stray /shim/* here is a 404, not a handler call.
            path = self.path.split("?", 1)[0]
            if path.startswith("/shim/"):
                self._send(404, {}, b"not found")
                return
            self._invoke_handler()

        def _invoke_handler(self) -> None:
            path = self.path.split("?", 1)[0]
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            if not state.is_ready() or state.handler is None:
                self._send(503, {}, b"shim not ready")
                return

            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""
            req_headers = {k: v for k, v in self.headers.items()}
            event = event_from_request(self.command, path, query, req_headers, raw_body)
            context = InvocationContext(invoke_path)
            # Deliberate HTTP error boundary: the except catches ANY handler
            # exception, logs the full traceback to stderr (the shipped guest log),
            # and returns it as a 502 (reported, not swallowed).
            try:  # nosemgrep: no-broad-except-swallow
                result = state.handler(event, context)
                status, headers, body = response_from_return(result)
            except Exception:  # noqa: BLE001 (any handler error is a 502)
                tb = traceback.format_exc()
                sys.stderr.write(tb)
                sys.stderr.flush()
                self._send(502, {"Content-Type": "text/plain"}, tb.encode("utf-8"))
                return
            self._send(status, headers, body)

        def _handle_hydrate(self) -> None:
            # Idempotency: a second hydrate after the shim is already ready is a
            # 409 Conflict (the base is built; re-delivering the archive is a
            # caller error, not a silent success). The build path hydrates once.
            if state.is_ready():
                self._send(409, {}, b"shim already hydrated")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            archive = self.rfile.read(length) if length else b""
            if not archive:
                self._send(400, {}, b"hydrate: empty archive body")
                return
            # Deliberate build-error boundary: the except catches ANY hydrate
            # failure (bad zip / zip-slip / import error), logs it, and reports a
            # 422; ready stays False (reported, not swallowed).
            try:  # nosemgrep: no-broad-except-swallow
                hydrate(state, archive)
            except Exception:  # noqa: BLE001 (bad zip / zip-slip / import error)
                tb = traceback.format_exc()
                sys.stderr.write("ember-shim: hydrate failed\n")
                sys.stderr.write(tb)
                sys.stderr.flush()
                # 422: the bytes were received fine but could not be turned into a
                # ready handler (unprocessable archive). ready stays False.
                self._send(422, {"Content-Type": "text/plain"}, tb.encode("utf-8"))
                return
            self._send(200, {}, b"hydrated")

    return ShimHTTPRequestHandler


class VsockHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer bound to an AF_VSOCK stream socket.

    http.server assumes AF_INET; overriding address_family and server_bind
    lets the same request handlers serve over vsock, matching the Go guest's
    listenVsock (bind VMADDR_CID_ANY:port). Firecracker forwards the host
    CONNECT handshake to this guest listener, after which it is plain HTTP/1.1.
    """

    # AF_VSOCK exists on Linux builds with vsock support (the guest always has
    # it). getattr keeps the module importable on hosts without vsock (e.g. the
    # macOS/CI test runner, which never instantiates this class); binding it
    # there would raise, but the shim only constructs VsockHTTPServer inside a
    # real guest.
    address_family = getattr(socket, "AF_VSOCK", -1)
    # allow_reuse_address uses SO_REUSEADDR, which is not meaningful on vsock;
    # leave it off so server_bind does not attempt an unsupported sockopt.
    allow_reuse_address = False

    def server_bind(self) -> None:
        # socketserver.TCPServer.server_bind sets SO_REUSEADDR and calls
        # getsockname to populate server_name/port. vsock getsockname returns
        # (cid, port); replicate the bind + address bookkeeping without the
        # TCP-only hostname resolution.
        self.socket.bind(self.server_address)
        cid, port = self.socket.getsockname()
        self.server_address = (cid, port)
        self.server_name = "vsock"
        self.server_port = port


def serve(server: ThreadingHTTPServer) -> None:
    """Run server.serve_forever until interrupted, closing on exit.

    Split out from main so tests can drive a plain TCP-backed server through
    the identical serve loop.
    """
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def exec_bootstrap(bootstrap_path: str) -> None:
    """Exec an adopter-supplied `bootstrap` executable, replacing this process.

    The bootstrap then owns serving the frozen contract. os.execv replaces the
    interpreter so PID 1 semantics carry over.
    """
    os.chdir(os.path.dirname(bootstrap_path))
    os.execv(bootstrap_path, [bootstrap_path])


def _serving_port() -> int | None:
    """Return the TCP serving port when in R3 serving mode, else None.

    A set, positive ``EMBER_SERVING_PORT`` selects serving (tap NIC / TCP) mode;
    unset, empty, non-numeric, or non-positive means the unchanged vsock path.
    Parsing is defensive so a malformed value degrades to the vsock path (the
    daemon's tap health probe would then fail loudly) rather than crashing PID 1.
    """
    raw = os.environ.get(SERVING_PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        sys.stderr.write(
            f"ember-shim: ignoring non-numeric {SERVING_PORT_ENV}={raw!r}\n"
        )
        return None
    return port if port > 0 else None


def build_server(
    state: "ShimState", invoke_path: str, serving_port: int | None
) -> ThreadingHTTPServer:
    """Build the boot server: AF_INET TCP in serving mode, else vsock.

    Both modes reuse the identical ``make_request_handler`` dispatch, so
    /shim/healthz, /shim/ready, and the invoke path behave the same over either
    transport. Serving mode binds a plain TCP socket on 0.0.0.0:<serving_port>
    (reached over the guest's tap NIC) and never binds vsock; the vsock path is
    byte-unchanged when serving_port is None.
    """
    request_handler_cls = make_request_handler(
        state, invoke_path, serving=serving_port is not None
    )
    if serving_port is not None:
        return ThreadingHTTPServer(("0.0.0.0", serving_port), request_handler_cls)  # noqa: S104
    port = int(os.environ.get("EMBER_HTTP_PORT", str(GUEST_HTTP_PORT)))
    return VsockHTTPServer((VMADDR_CID_ANY, port), request_handler_cls)


def read_handler_zip_from_device(device: str, nbytes: int) -> bytes:
    """Read EXACTLY nbytes of zip payload from a handler-disk block device.

    The handler artifact is a raw zip on a block device (D-R3.11.2), which is
    sector-padded: the device is longer than the zip and the tail is zero padding.
    Reading the whole device and handing it to zipfile fails, because zipfile scans
    backward from the end for the EOCD signature and the padding hides it. noded
    conveys the exact zip length (EMBER_HANDLER_ZIP_BYTES) so we read only the
    payload. A short read (device smaller than nbytes) is an error: the artifact is
    truncated and the handler cannot be trusted.
    """
    with open(device, "rb") as f:  # noqa: PTH123 (block device, not a path-y file)
        data = f.read(nbytes)
    if len(data) != nbytes:
        raise ValueError(
            f"handler disk {device!r}: read {len(data)} bytes, expected {nbytes}"
        )
    return data


def _cold_boot_hydrate(state: ShimState) -> None:
    """Import the handler off the handler-disk before serving (R3, D-R3.11.2).

    When EMBER_HANDLER_ZIP names a block device, read exactly EMBER_HANDLER_ZIP_BYTES
    from it and run the SAME hydrate() the build-time POST /shim/hydrate runs, so a
    serving cold boot has its handler imported and /shim/ready answers 200 at the
    first probe. A missing/zero byte count or an unreadable/invalid device raises,
    which main() turns into a NON-ZERO exit: a serving guest that cannot import its
    handler must crash the boot, not serve 503 forever (finishServingStart would reap
    it either way, but a loud exit surfaces the cause in the guest console).
    """
    device = os.environ.get(HANDLER_ZIP_ENV, "").strip()
    if not device:
        return
    raw_bytes = os.environ.get(HANDLER_ZIP_BYTES_ENV, "").strip()
    if not raw_bytes:
        raise ValueError(f"{HANDLER_ZIP_ENV} set without {HANDLER_ZIP_BYTES_ENV}")
    nbytes = int(raw_bytes)
    if nbytes <= 0:
        raise ValueError(f"{HANDLER_ZIP_BYTES_ENV}={raw_bytes!r} must be positive")
    archive = read_handler_zip_from_device(device, nbytes)
    # hydrate() unpacks + imports and flips state.ready True (or execs bootstrap for
    # the any-language escape hatch, which never returns).
    hydrate(state, archive)


def main() -> int:
    handler_symbol = os.environ.get("EMBER_HANDLER", DEFAULT_HANDLER)
    invoke_path = os.environ.get("EMBER_INVOKE_PATH", DEFAULT_INVOKE_PATH)
    serving_port = _serving_port()

    # Boot un-hydrated: serve immediately (so Prime's healthz probe answers) but
    # report not-ready until the handler is imported. There are two import paths:
    #   - TASK/SESSION (vsock): a build-time POST /shim/hydrate imports the handler,
    #     then the base is snapshotted with the handler live; restores are ready.
    #   - SERVING (tap NIC): a serving VM COLD-BOOTS with a NIC (D-R3.4.2) and cannot
    #     resume that memory snapshot, so it imports the handler off the handler-disk
    #     here, before serving (D-R3.11.2, _cold_boot_hydrate). No runtime network
    #     hydrate is involved either way; the zip stays build-time-only.
    state = ShimState(invoke_path=invoke_path, handler_symbol=handler_symbol)

    # Serving cold boot: import the handler off the handler-disk before binding the
    # socket, so /shim/ready is 200 at the first probe. A failure crashes the boot
    # (non-zero exit) rather than serving a permanently-not-ready guest.
    _cold_boot_hydrate(state)

    server = build_server(state, invoke_path, serving_port)
    if serving_port is not None:
        ready = "ready" if state.is_ready() else "awaiting hydrate"
        sys.stderr.write(
            f"ember-shim: serving on TCP 0.0.0.0:{serving_port} invokePath={invoke_path} "
            f"handler={handler_symbol} ({ready})\n"
        )
    else:
        sys.stderr.write(
            f"ember-shim: serving on vsock port {server.server_port} invokePath={invoke_path} "
            f"handler={handler_symbol} (awaiting hydrate)\n"
        )
    sys.stderr.flush()
    serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
