"""EmberVM python runtime bootstrap shim (ADR embervm/002, R1 zip lane).

This shim runs inside a disposable python-runtime microVM, launched by the guest
init (a raw Firecracker boot ignores the OCI entrypoint, so Task 7 must wire a
PID-1 harnessInit that mounts a tmpfs over /tmp then execs this shim, see README
"Boot integration"). An adopter of the zip lane brings only a zip archive
containing a `handler(event, context)` callable; noded attaches that archive as a
read-only block device (default /dev/vdb, Task 6) and boots this base image. On
boot the shim, entirely guest-side:

  1. Locates the archive block device and unpacks it into a tmpfs workdir.
     The host NEVER unpacks: the archive crosses the boundary as opaque bytes
     and is expanded only here, inside the throwaway guest.
  2. If the archive root holds an executable file named `bootstrap`, execs it
     instead of importing python (the any-language escape hatch). That process
     is then responsible for serving the same frozen guest contract.
  3. Otherwise imports the configured handler symbol (env EMBER_HANDLER, default
     `app.handle`) and serves the frozen guest contract over HTTP on vsock port
     1027 (embervm noded's GuestHTTPPort):
       - GET  /shim/healthz  -> 200 always (liveness; noded's Prime probe).
       - GET  /shim/ready    -> 200 ONLY after a successful handler import,
                                503 otherwise. noded's BuildBase health-gates on
                                this, so an import failure keeps the base VM from
                                ever going ready and the error surfaces later in
                                the Workload condition.
       - POST <invokePath>   -> marshal the HTTP request into an event dict,
                                call handler(event, context), marshal the return
                                into the HTTP response.

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
import json
import os
import socket
import stat
import sys
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

# Env-configured knobs, all read at boot with sane defaults (documented in
# README.md). EMBER_INVOKE_PATH defaults to /invoke, matching noded's
# server.defaultInvokePath, NOT "/" (a request that carries no path falls back
# to /invoke on the host side, so the shim must listen there by default).
DEFAULT_HANDLER = "app.handle"
DEFAULT_ARCHIVE_DEVICE = "/dev/vdb"
DEFAULT_INVOKE_PATH = "/invoke"

# Where the archive is unpacked. A tmpfs mount is expected over /tmp (the rootfs
# is read-only and shared across restores); the workdir lives under it so the
# unpacked code is writable and per-guest.
UNPACK_DIR = "/tmp/ember-app"

READY_PATH = "/shim/ready"
HEALTHZ_PATH = "/shim/healthz"


class InvocationContext:
    """The `context` argument passed to handler(event, context).

    Deliberately minimal. Adopters that need request metadata read it from the
    event; this object only exposes the invoke path so a handler can sub-route,
    and stays open for extension without breaking the frozen shape.
    """

    def __init__(self, invoke_path: str) -> None:
        self.invoke_path = invoke_path


def find_archive_device(configured: str) -> str:
    """Return the archive block-device path to read the zip from.

    The configured path (env EMBER_ARCHIVE_DEVICE, default /dev/vdb) is used
    as-is when it exists. It is returned even when it is not a block device so
    tests can point it at a plain file; the caller (unpack_archive) validates
    the contents are a real zip and fails loudly otherwise.
    """
    if not configured:
        raise ValueError("archive device path is empty")
    if not os.path.exists(configured):
        raise FileNotFoundError(f"archive device not found: {configured}")
    return configured


def _is_within(base: str, target: str) -> bool:
    """True iff target resolves inside base (zip-slip guard).

    Uses realpath on both so a "../" entry or an absolute member path that
    escapes base is rejected. base itself is treated as inside.
    """
    base_real = os.path.realpath(base)
    target_real = os.path.realpath(target)
    return target_real == base_real or target_real.startswith(base_real + os.sep)


def unpack_archive(device_path: str, dest: str) -> str:
    """Unpack the zip at device_path into dest and return dest.

    Rejects zip-slip: any member whose destination escapes dest (via "../" or
    an absolute path) aborts the whole unpack with a ValueError. This runs
    guest-side only; the host never sees the expanded tree.
    """
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(device_path) as zf:
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
    the caller turns any raise into a non-ready shim (ready never flips).
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
    handler: Callable[[Any, Any], Any] | None,
    ready: Callable[[], bool],
    invoke_path: str,
) -> type[BaseHTTPRequestHandler]:
    """Build the BaseHTTPRequestHandler class serving the frozen contract.

    handler may be None (import failed): the control routes still answer, but
    /shim/ready reports 503 via `ready`, and any POST returns 503 too.
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
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            path = self.path.split("?", 1)[0]
            if path == HEALTHZ_PATH:
                self._send(200, {}, b"")
                return
            if path == READY_PATH:
                if ready():
                    self._send(200, {}, b"")
                else:
                    self._send(503, {}, b"shim not ready: handler import failed")
                return
            self._send(404, {}, b"not found")

        def do_POST(self) -> None:  # noqa: N802 (http.server naming)
            path = self.path.split("?", 1)[0]
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            if path != invoke_path:
                self._send(404, {}, b"not found")
                return
            if handler is None or not ready():
                self._send(503, {}, b"shim not ready")
                return

            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""
            req_headers = {k: v for k, v in self.headers.items()}
            event = event_from_request(self.command, path, query, req_headers, raw_body)
            context = InvocationContext(invoke_path)
            try:
                result = handler(event, context)
                status, headers, body = response_from_return(result)
            except Exception:  # noqa: BLE001 (any handler error is a 502)
                tb = traceback.format_exc()
                sys.stderr.write(tb)
                sys.stderr.flush()
                self._send(502, {"Content-Type": "text/plain"}, tb.encode("utf-8"))
                return
            self._send(status, headers, body)

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


def main() -> int:
    handler_symbol = os.environ.get("EMBER_HANDLER", DEFAULT_HANDLER)
    device = os.environ.get("EMBER_ARCHIVE_DEVICE", DEFAULT_ARCHIVE_DEVICE)
    invoke_path = os.environ.get("EMBER_INVOKE_PATH", DEFAULT_INVOKE_PATH)
    port = int(os.environ.get("EMBER_HTTP_PORT", str(GUEST_HTTP_PORT)))

    handler: Callable[[Any, Any], Any] | None = None
    try:
        resolved_device = find_archive_device(device)
        app_dir = unpack_archive(resolved_device, UNPACK_DIR)

        bootstrap = _find_bootstrap(app_dir)
        if bootstrap is not None:
            # Any-language escape hatch: hand the whole guest over to bootstrap.
            # It never returns; it must serve the contract itself.
            sys.stderr.write(f"ember-shim: exec bootstrap {bootstrap}\n")
            sys.stderr.flush()
            exec_bootstrap(bootstrap)
            return 0  # unreachable; execv replaced the process

        handler = load_handler(app_dir, handler_symbol)
    except Exception:  # noqa: BLE001
        # Import/unpack failure: write the traceback for guest-log shipping and
        # keep serving with handler=None so /shim/ready reports 503 forever.
        # BuildBase health-gates on ready, so the base never goes ready and the
        # failure surfaces in the Workload condition instead of hanging silently.
        tb = traceback.format_exc()
        sys.stderr.write("ember-shim: handler bootstrap failed\n")
        sys.stderr.write(tb)
        sys.stderr.flush()

    # ready() closes over the resolved handler: true only once import succeeded.
    def ready() -> bool:
        return handler is not None

    request_handler_cls = make_request_handler(handler, ready, invoke_path)
    server = VsockHTTPServer((VMADDR_CID_ANY, port), request_handler_cls)
    sys.stderr.write(
        f"ember-shim: serving on vsock port {port} invokePath={invoke_path} "
        f"ready={handler is not None}\n"
    )
    sys.stderr.flush()
    serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
