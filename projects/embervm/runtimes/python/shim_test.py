"""Unit tests for the EmberVM python runtime bootstrap shim (R1 zip lane).

The shim is stdlib-only plain python, so these run without a microVM, without
vsock, and without the baked runtime image: unpack from raw bytes (including a
zip-slip rejection), handler import success/failure, event/response marshaling in
both directions (including a base64 binary body), bootstrap-override selection,
the vsock-hydration flow (POST /shim/hydrate -> ready -> invoke echoes; a bad
archive leaves ready False; invoke-before-hydrate is 503), and an end-to-end HTTP
round-trip over a plain TCP socket standing in for vsock.
"""

from __future__ import annotations

import base64
import io
import os
import stat
import sys
import threading
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import shim


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# unpack (raw bytes; no block device, no padding)
# ---------------------------------------------------------------------------


def test_unpack_extracts_members(tmp_path):
    archive = _zip_bytes({"app.py": b"x = 1\n", "pkg/mod.py": b"y = 2\n"})
    dest = tmp_path / "out"

    shim.unpack_archive(archive, str(dest))

    assert (dest / "app.py").read_bytes() == b"x = 1\n"
    assert (dest / "pkg" / "mod.py").read_bytes() == b"y = 2\n"


def test_unpack_accepts_bytesio(tmp_path):
    archive = _zip_bytes({"app.py": b"z = 3\n"})
    dest = tmp_path / "out"

    shim.unpack_archive(io.BytesIO(archive), str(dest))

    assert (dest / "app.py").read_bytes() == b"z = 3\n"


def test_unpack_bad_zip_raises(tmp_path):
    dest = tmp_path / "out"
    with pytest.raises(zipfile.BadZipFile):
        shim.unpack_archive(b"\x00" * 4096, str(dest))


def test_unpack_rejects_zip_slip(tmp_path):
    # A member with a traversal path must abort the whole unpack, never writing
    # outside dest.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.py", b"pwned = True\n")
    dest = tmp_path / "out"

    with pytest.raises(ValueError, match="zip-slip"):
        shim.unpack_archive(buf.getvalue(), str(dest))

    # The escape target must not exist.
    assert not (tmp_path / "escape.py").exists()


# ---------------------------------------------------------------------------
# handler import
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_imports():
    """Snapshot sys.path and sys.modules so per-test app modules do not leak.

    load_handler prepends app_dir to sys.path and imports by module name; two
    tests both importing a module named "app" from different dirs would
    otherwise collide on the import cache. Restore both after each test.
    """
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    yield
    sys.path[:] = saved_path
    for name in set(sys.modules) - saved_modules:
        del sys.modules[name]


def test_load_handler_success(tmp_path, isolated_imports):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "handler_ok.py").write_text(
        "def handle(event, context):\n    return {'statusCode': 200, 'body': 'ok'}\n"
    )

    handler = shim.load_handler(str(app_dir), "handler_ok.handle")
    assert callable(handler)
    assert handler({}, None)["body"] == "ok"


def test_load_handler_missing_module(tmp_path, isolated_imports):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    with pytest.raises(ModuleNotFoundError):
        shim.load_handler(str(app_dir), "nosuch.handle")


def test_load_handler_missing_attr(tmp_path, isolated_imports):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "handler_noattr.py").write_text("x = 1\n")
    with pytest.raises(AttributeError):
        shim.load_handler(str(app_dir), "handler_noattr.handle")


def test_load_handler_not_callable(tmp_path, isolated_imports):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "handler_notcallable.py").write_text("handle = 42\n")
    with pytest.raises(TypeError):
        shim.load_handler(str(app_dir), "handler_notcallable.handle")


def test_load_handler_bad_symbol(tmp_path, isolated_imports):
    with pytest.raises(ValueError, match="module.attr"):
        shim.load_handler(str(tmp_path), "nodots")


# ---------------------------------------------------------------------------
# bootstrap override selection
# ---------------------------------------------------------------------------


def test_find_bootstrap_executable(tmp_path):
    boot = tmp_path / "bootstrap"
    boot.write_text("#!/bin/sh\necho hi\n")
    boot.chmod(boot.stat().st_mode | stat.S_IXUSR)
    assert shim._find_bootstrap(str(tmp_path)) == str(boot)


def test_find_bootstrap_non_executable_ignored(tmp_path):
    boot = tmp_path / "bootstrap"
    boot.write_text("not executable\n")
    boot.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert shim._find_bootstrap(str(tmp_path)) is None


def test_find_bootstrap_absent(tmp_path):
    assert shim._find_bootstrap(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# event marshaling (request -> event)
# ---------------------------------------------------------------------------


def test_event_from_request_text_body():
    event = shim.event_from_request(
        "POST", "/invoke", "a=1&b=2", {"Content-Type": "text/plain"}, b"hello"
    )
    assert event["httpMethod"] == "POST"
    assert event["path"] == "/invoke"
    assert event["queryStringParameters"] == {"a": "1", "b": "2"}
    assert event["body"] == "hello"
    assert event["isBase64Encoded"] is False


def test_event_from_request_binary_body_base64():
    # A non-UTF-8 payload must round-trip as base64 with the flag set.
    binary = bytes([0xFF, 0x00, 0xFE, 0x80])
    event = shim.event_from_request("POST", "/invoke", "", {}, binary)
    assert event["isBase64Encoded"] is True
    assert base64.b64decode(event["body"]) == binary


def test_event_from_request_empty_body_is_none():
    event = shim.event_from_request("GET", "/invoke", "", {}, b"")
    assert event["body"] is None
    assert event["isBase64Encoded"] is False
    assert event["queryStringParameters"] is None


# ---------------------------------------------------------------------------
# response marshaling (return -> response)
# ---------------------------------------------------------------------------


def test_response_from_return_dict():
    status, headers, body = shim.response_from_return(
        {"statusCode": 201, "headers": {"X-A": "b"}, "body": "created"}
    )
    assert status == 201
    assert headers == {"X-A": "b"}
    assert body == b"created"


def test_response_from_return_base64_body():
    binary = bytes([0x01, 0x02, 0xFF])
    status, _headers, body = shim.response_from_return(
        {
            "statusCode": 200,
            "body": base64.b64encode(binary).decode("ascii"),
            "isBase64Encoded": True,
        }
    )
    assert status == 200
    assert body == binary


def test_response_from_return_default_status():
    status, _headers, body = shim.response_from_return({"body": "x"})
    assert status == 200
    assert body == b"x"


def test_response_from_return_bare_string():
    status, _headers, body = shim.response_from_return("plain")
    assert status == 200
    assert body == b"plain"


def test_response_from_return_json_payload():
    status, headers, body = shim.response_from_return(
        {"statusCode": 200, "body": {"k": 1}}
    )
    # A dict body without base64 is JSON-encoded.
    assert status == 200
    assert body == b'{"k": 1}'


# ---------------------------------------------------------------------------
# hydrate flow (unit-level, no HTTP)
# ---------------------------------------------------------------------------


def _fresh_state(tmp_path, monkeypatch, handler_symbol="app.handle"):
    """A ShimState whose UNPACK_DIR is redirected under tmp_path.

    hydrate() unpacks into shim.UNPACK_DIR (/tmp/ember-app in prod); point it at
    a per-test dir so tests do not collide or need a writable /tmp/ember-app.
    """
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    return shim.ShimState(invoke_path="/invoke", handler_symbol=handler_symbol)


def test_hydrate_imports_and_flips_ready(tmp_path, monkeypatch, isolated_imports):
    state = _fresh_state(tmp_path, monkeypatch, "app.handle")
    archive = _zip_bytes(
        {"app.py": b"def handle(event, context):\n    return {'body': 'ok'}\n"}
    )
    assert state.is_ready() is False

    shim.hydrate(state, archive)

    assert state.is_ready() is True
    assert state.hydrated is True
    assert callable(state.handler)


def test_hydrate_bad_zip_leaves_not_ready(tmp_path, monkeypatch, isolated_imports):
    state = _fresh_state(tmp_path, monkeypatch)
    with pytest.raises(zipfile.BadZipFile):
        shim.hydrate(state, b"not a zip at all")
    assert state.is_ready() is False
    assert state.handler is None


def test_hydrate_import_error_leaves_not_ready(tmp_path, monkeypatch, isolated_imports):
    state = _fresh_state(tmp_path, monkeypatch, "app.handle")
    # A syntactically broken module fails to import: hydrate raises, ready False.
    archive = _zip_bytes({"app.py": b"this is not valid python :::\n"})
    with pytest.raises(SyntaxError):
        shim.hydrate(state, archive)
    assert state.is_ready() is False


def test_hydrate_rejects_zip_slip(tmp_path, monkeypatch, isolated_imports):
    state = _fresh_state(tmp_path, monkeypatch)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.py", b"pwned = True\n")
    with pytest.raises(ValueError, match="zip-slip"):
        shim.hydrate(state, buf.getvalue())
    assert state.is_ready() is False


def test_hydrate_selects_bootstrap_override(tmp_path, monkeypatch, isolated_imports):
    # An executable `bootstrap` at the archive root is exec'd instead of importing
    # a python handler. hydrate calls exec_bootstrap, which would replace the
    # process; stub it so the test observes the selection without an execv.
    #
    # _find_bootstrap's own executable-bit detection is covered by
    # test_find_bootstrap_executable (a real chmod'd file); here we stub it to
    # return a path, because zipfile.extractall's restoration of the Unix exec bit
    # from external_attr varies by Python version (3.14 drops it), and this test is
    # about the hydrate BRANCH, not zip mode preservation.
    state = _fresh_state(tmp_path, monkeypatch)
    execed: dict[str, str] = {}

    monkeypatch.setattr(
        shim, "exec_bootstrap", lambda path: execed.setdefault("path", path)
    )
    monkeypatch.setattr(
        shim, "_find_bootstrap", lambda app_dir: os.path.join(app_dir, "bootstrap")
    )

    archive = _zip_bytes({"bootstrap": b"#!/bin/sh\necho hi\n"})
    shim.hydrate(state, archive)

    assert execed["path"].endswith("/bootstrap")
    # bootstrap owns readiness after execv; the python-import path did not flip it.
    assert state.handler is None
    assert state.is_ready() is False


# ---------------------------------------------------------------------------
# HTTP serving round-trip (TCP stands in for vsock)
# ---------------------------------------------------------------------------


def _run_server(state, invoke_path="/invoke"):
    """Start a TCP-backed ThreadingHTTPServer using the shim's request handler.

    The production server binds AF_VSOCK; the request-handling logic under test
    is identical, so a loopback TCP server exercises the full contract without
    the vsock kernel module (absent on CI runners).
    """
    cls = shim.make_request_handler(state, invoke_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _new_state(invoke_path="/invoke", handler_symbol="app.handle"):
    return shim.ShimState(invoke_path=invoke_path, handler_symbol=handler_symbol)


def test_healthz_always_200():
    # healthz answers 200 even before hydration (Prime's liveness probe).
    server, _thread = _run_server(_new_state())
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.HEALTHZ_PATH)
        assert conn.getresponse().status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_ready_503_before_hydrate():
    server, _t = _run_server(_new_state())
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.READY_PATH)
        assert conn.getresponse().status == 503
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_before_hydrate_is_503():
    server, _t = _run_server(_new_state())
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=b"x")
        assert conn.getresponse().status == 503
    finally:
        server.shutdown()
        server.server_close()


def test_hydrate_then_ready_then_invoke(tmp_path, monkeypatch, isolated_imports):
    # The end-to-end build flow over HTTP: POST /shim/hydrate delivers the archive,
    # /shim/ready flips to 200, and the invoke path then echoes through the handler.
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    state = _new_state()
    server, _t = _run_server(state)
    try:
        archive = _zip_bytes(
            {
                "app.py": (
                    b"def handle(event, context):\n"
                    b"    return {'statusCode': 200, 'body': 'echo:' + (event['body'] or '')}\n"
                )
            }
        )

        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=archive)
        assert conn.getresponse().status == 200

        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.READY_PATH)
        assert conn.getresponse().status == 200

        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=b"ping")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == b"echo:ping"
    finally:
        server.shutdown()
        server.server_close()


def test_hydrate_bad_zip_over_http_leaves_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    state = _new_state()
    server, _t = _run_server(state)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=b"not a zip")
        assert conn.getresponse().status == 422

        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.READY_PATH)
        assert conn.getresponse().status == 503
    finally:
        server.shutdown()
        server.server_close()


def test_hydrate_empty_body_is_400(tmp_path, monkeypatch):
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    state = _new_state()
    server, _t = _run_server(state)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=b"")
        assert conn.getresponse().status == 400
    finally:
        server.shutdown()
        server.server_close()


def test_second_hydrate_after_ready_is_409(tmp_path, monkeypatch, isolated_imports):
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    state = _new_state()
    server, _t = _run_server(state)
    try:
        archive = _zip_bytes(
            {"app.py": b"def handle(event, context):\n    return {'body': 'ok'}\n"}
        )
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=archive)
        assert conn.getresponse().status == 200

        # A repeat hydrate once ready is a 409 Conflict (idempotency choice).
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=archive)
        assert conn.getresponse().status == 409
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_binary_body_round_trip(tmp_path, monkeypatch, isolated_imports):
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    state = _new_state()
    server, _t = _run_server(state)
    binary = bytes(range(256))
    try:
        # A handler that echoes the (base64) body straight back, so binary bytes
        # survive the JSON marshal in both directions.
        archive = _zip_bytes(
            {
                "app.py": (
                    b"def handle(event, context):\n"
                    b"    return {'statusCode': 200, 'body': event['body'],"
                    b" 'isBase64Encoded': event['isBase64Encoded']}\n"
                )
            }
        )
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=archive)
        assert conn.getresponse().status == 200

        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=binary)
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == binary
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# R3 serving mode (D-R3.11.1): EMBER_SERVING_PORT selects an AF_INET TCP bind
# over the tap NIC instead of vsock, reusing the identical request handler. The
# vsock path (env unset) stays byte-identical for task/session guests.
# ---------------------------------------------------------------------------


def test_serving_port_unset_is_none(monkeypatch):
    monkeypatch.delenv(shim.SERVING_PORT_ENV, raising=False)
    assert shim._serving_port() is None


def test_serving_port_parses_positive(monkeypatch):
    monkeypatch.setenv(shim.SERVING_PORT_ENV, "8080")
    assert shim._serving_port() == 8080


@pytest.mark.parametrize("bad", ["", "  ", "0", "-1", "notaport"])
def test_serving_port_malformed_degrades_to_vsock(monkeypatch, bad):
    # A missing, non-numeric, or non-positive value means "no serving mode": the
    # boot falls back to vsock rather than crashing PID 1 (the daemon's tap probe
    # then fails loudly instead of the guest dying at boot).
    monkeypatch.setenv(shim.SERVING_PORT_ENV, bad)
    assert shim._serving_port() is None


def test_build_server_vsock_when_not_serving(monkeypatch):
    # No serving port -> the production vsock server (VsockHTTPServer). AF_VSOCK
    # cannot bind on the CI/mac runner, so stub the bind/activate to prove the
    # SELECTION without a live vsock bind: the class chosen is what matters.
    monkeypatch.delenv(shim.SERVING_PORT_ENV, raising=False)
    monkeypatch.setattr(shim.VsockHTTPServer, "server_bind", lambda self: None)
    monkeypatch.setattr(shim.VsockHTTPServer, "server_activate", lambda self: None)
    server = shim.build_server(_new_state(), "/invoke", None)
    assert server.__class__ is shim.VsockHTTPServer


def test_build_server_binds_tcp_in_serving_mode():
    # Serving mode binds a real AF_INET socket (port 0 = ephemeral) with the SAME
    # handler, so /shim/healthz answers 200 over plain TCP (what noded's tap probe
    # hits: GET http://ip:port/shim/healthz).
    server = shim.build_server(_new_state(), "/invoke", 0)
    assert not isinstance(server, shim.VsockHTTPServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.HEALTHZ_PATH)
        assert conn.getresponse().status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_handler_exception_is_502(tmp_path, monkeypatch, isolated_imports):
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    state = _new_state()
    server, _t = _run_server(state)
    try:
        archive = _zip_bytes(
            {
                "app.py": (
                    b"def handle(event, context):\n    raise RuntimeError('boom')\n"
                )
            }
        )
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=archive)
        assert conn.getresponse().status == 200

        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=b"x")
        resp = conn.getresponse()
        assert resp.status == 502
        assert b"boom" in resp.read()
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_wrong_path_is_404(tmp_path, monkeypatch, isolated_imports):
    monkeypatch.setattr(shim, "UNPACK_DIR", str(tmp_path / "ember-app"))
    state = _new_state()
    server, _t = _run_server(state)
    try:
        archive = _zip_bytes(
            {"app.py": b"def handle(event, context):\n    return 'ok'\n"}
        )
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", shim.HYDRATE_PATH, body=archive)
        assert conn.getresponse().status == 200

        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/nope", body=b"x")
        assert conn.getresponse().status == 404
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# cold-boot handler import off a (sector-padded) block device (D-R3.11.2)
# ---------------------------------------------------------------------------


def test_read_handler_zip_from_device_trims_sector_padding(tmp_path):
    """The killer case: a block device is longer than the zip and the tail is zero
    padding. Reading EXACTLY the zip length recovers a valid archive regardless of
    how much padding follows; reading the WHOLE device breaks zipfile once the
    padding exceeds its backward EOCD-scan window. The exact byte count is the
    durable defence (the retired R1 sector-pad/EOCD bug class).

    Python's zipfile only scans the last ~64 KiB for the EOCD signature, so a small
    zero tail is tolerated but a large one is not; a real handler drive's padding is
    unbounded (Firecracker never shrinks a drive file), so reading exactly N bytes is
    the only size-independent guarantee. We pad past the 64 KiB window to make the
    whole-device read fail deterministically here.
    """
    archive = _zip_bytes({"app.py": b"x = 1\n"})
    device = tmp_path / "vdb"
    padded = archive + b"\x00" * (70 * 1024)  # beyond zipfile's ~64 KiB EOCD scan
    device.write_bytes(padded)
    assert len(padded) > len(archive)  # there IS padding to trip on

    # Reading the whole padded device is NOT a valid zip (the retired bug): the EOCD
    # is hidden past the backward scan window.
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(padded))

    # Reading exactly len(archive) bytes recovers the zip, padding notwithstanding.
    got = shim.read_handler_zip_from_device(str(device), len(archive))
    assert got == archive
    assert zipfile.ZipFile(io.BytesIO(got)).namelist() == ["app.py"]


def test_read_handler_zip_from_device_short_read_raises(tmp_path):
    device = tmp_path / "vdb"
    device.write_bytes(b"abc")
    with pytest.raises(ValueError):
        shim.read_handler_zip_from_device(str(device), 4096)


def test_cold_boot_hydrate_imports_from_device(tmp_path, monkeypatch, isolated_imports):
    """The full cold-boot import path: EMBER_HANDLER_ZIP names a padded device and
    EMBER_HANDLER_ZIP_BYTES its exact length; _cold_boot_hydrate imports the handler
    and flips ready, so a serving guest is ready without a network hydrate."""
    state = _fresh_state(tmp_path, monkeypatch, "app.handle")
    archive = _zip_bytes(
        {"app.py": b"def handle(event, context):\n    return {'body': 'ok'}\n"}
    )
    device = tmp_path / "vdb"
    device.write_bytes(archive + b"\x00" * 512)  # sector padding
    monkeypatch.setenv(shim.HANDLER_ZIP_ENV, str(device))
    monkeypatch.setenv(shim.HANDLER_ZIP_BYTES_ENV, str(len(archive)))

    assert state.is_ready() is False
    shim._cold_boot_hydrate(state)
    assert state.is_ready() is True


def test_cold_boot_hydrate_noop_without_env(tmp_path, monkeypatch):
    """No EMBER_HANDLER_ZIP (task/session/relight boot): a no-op, state stays
    un-ready and no device is read."""
    state = _fresh_state(tmp_path, monkeypatch, "app.handle")
    monkeypatch.delenv(shim.HANDLER_ZIP_ENV, raising=False)
    shim._cold_boot_hydrate(state)
    assert state.is_ready() is False


def test_cold_boot_hydrate_missing_byte_count_raises(tmp_path, monkeypatch):
    state = _fresh_state(tmp_path, monkeypatch, "app.handle")
    device = tmp_path / "vdb"
    device.write_bytes(_zip_bytes({"app.py": b"x = 1\n"}))
    monkeypatch.setenv(shim.HANDLER_ZIP_ENV, str(device))
    monkeypatch.delenv(shim.HANDLER_ZIP_BYTES_ENV, raising=False)
    with pytest.raises(ValueError):
        shim._cold_boot_hydrate(state)
