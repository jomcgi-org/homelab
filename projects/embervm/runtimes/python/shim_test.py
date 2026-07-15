"""Unit tests for the EmberVM python runtime bootstrap shim (R1 zip lane).

The shim is stdlib-only plain python, so these run without a microVM, without
vsock, and without the baked runtime image: unpack (including a zip-slip
rejection), handler import success/failure, event/response marshaling in both
directions (including a base64 binary body), bootstrap-override selection, and
an end-to-end HTTP round-trip over a plain TCP socket standing in for vsock.
"""

from __future__ import annotations

import base64
import stat
import sys
import threading
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import shim


def _write_zip(path: str, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------


def test_unpack_extracts_members(tmp_path):
    archive = tmp_path / "app.zip"
    _write_zip(str(archive), {"app.py": b"x = 1\n", "pkg/mod.py": b"y = 2\n"})
    dest = tmp_path / "out"

    shim.unpack_archive(str(archive), str(dest))

    assert (dest / "app.py").read_bytes() == b"x = 1\n"
    assert (dest / "pkg" / "mod.py").read_bytes() == b"y = 2\n"


def test_unpack_rejects_zip_slip(tmp_path):
    # A member with a traversal path must abort the whole unpack, never writing
    # outside dest.
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(archive), "w") as zf:
        zf.writestr("../escape.py", b"pwned = True\n")
    dest = tmp_path / "out"

    with pytest.raises(ValueError, match="zip-slip"):
        shim.unpack_archive(str(archive), str(dest))

    # The escape target must not exist.
    assert not (tmp_path / "escape.py").exists()


def test_find_archive_device_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        shim.find_archive_device(str(tmp_path / "nope"))


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
# HTTP serving round-trip (TCP stands in for vsock)
# ---------------------------------------------------------------------------


def _run_server(handler, ready, invoke_path="/invoke"):
    """Start a TCP-backed ThreadingHTTPServer using the shim's request handler.

    The production server binds AF_VSOCK; the request-handling logic under test
    is identical, so a loopback TCP server exercises the full contract without
    the vsock kernel module (absent on CI runners).
    """
    cls = shim.make_request_handler(handler, ready, invoke_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_healthz_always_200():
    server, _thread = _run_server(handler=None, ready=lambda: False)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.HEALTHZ_PATH)
        assert conn.getresponse().status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_ready_reflects_import():
    # not ready
    server, _t = _run_server(handler=None, ready=lambda: False)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.READY_PATH)
        assert conn.getresponse().status == 503
    finally:
        server.shutdown()
        server.server_close()

    # ready
    def handler(event, context):
        return {"statusCode": 200, "body": "ok"}

    server, _t = _run_server(handler=handler, ready=lambda: True)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", shim.READY_PATH)
        assert conn.getresponse().status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_round_trip():
    def handler(event, context):
        assert event["httpMethod"] == "POST"
        assert event["body"] == "ping"
        return {"statusCode": 202, "headers": {"X-Echo": "1"}, "body": "pong"}

    server, _t = _run_server(handler=handler, ready=lambda: True)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=b"ping")
        resp = conn.getresponse()
        assert resp.status == 202
        assert resp.getheader("X-Echo") == "1"
        assert resp.read() == b"pong"
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_binary_body_round_trip():
    binary = bytes(range(256))

    def handler(event, context):
        assert event["isBase64Encoded"] is True
        received = base64.b64decode(event["body"])
        # echo the bytes back base64-encoded
        return {
            "statusCode": 200,
            "body": base64.b64encode(received).decode("ascii"),
            "isBase64Encoded": True,
        }

    server, _t = _run_server(handler=handler, ready=lambda: True)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=binary)
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == binary
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_handler_exception_is_502():
    def handler(event, context):
        raise RuntimeError("boom")

    server, _t = _run_server(handler=handler, ready=lambda: True)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=b"x")
        resp = conn.getresponse()
        assert resp.status == 502
        assert b"boom" in resp.read()
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_when_not_ready_is_503():
    server, _t = _run_server(handler=None, ready=lambda: False)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/invoke", body=b"x")
        assert conn.getresponse().status == 503
    finally:
        server.shutdown()
        server.server_close()


def test_invoke_wrong_path_is_404():
    server, _t = _run_server(handler=lambda e, c: "ok", ready=lambda: True)
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/nope", body=b"x")
        assert conn.getresponse().status == 404
    finally:
        server.shutdown()
        server.server_close()
