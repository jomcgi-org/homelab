from __future__ import annotations

import asyncio
import datetime
import os
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from factory.execution import broker_client


def _ca(serial: int) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")]))
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return certificate, key


def _identity(
    directory: Path,
    prefix: str,
    ca: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    spiffe_id: str,
    serial: int,
    *,
    server: bool,
    additional_spiffe_ids: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    usage = (
        ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(identity)
                    for identity in (spiffe_id, *additional_spiffe_ids)
                ]
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = directory / f"{prefix}.pem"
    key_path = directory / f"{prefix}_key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _write_bundle(path: Path, ca: x509.Certificate) -> None:
    path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))


def _serve(
    directory: Path,
    server_id: str,
    ca: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    serial: int,
    additional_spiffe_ids: tuple[str, ...] = (),
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server_cert, server_key = _identity(
        directory,
        f"server-{serial}",
        ca,
        ca_key,
        server_id,
        serial,
        server=True,
        additional_spiffe_ids=additional_spiffe_ids,
    )
    bundle = directory / f"server-bundle-{serial}.pem"
    _write_bundle(bundle, ca)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(server_cert, server_key)
    context.load_verify_locations(cafile=bundle)
    context.verify_mode = ssl.CERT_REQUIRED
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class _Handler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, int]] = []

    def _reply(self) -> None:
        peer = x509.load_der_x509_certificate(
            self.connection.getpeercert(binary_form=True)
        )
        type(self).calls.append((self.command, self.path, peer.serial_number))
        phase = getattr(self.server, "slow_stream_phase", None)
        if phase:
            try:
                if phase == "headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                else:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n")
                for _ in range(30):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
                if phase == "headers":
                    self.wfile.write(b"\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                # The bounded client closes the socket before this drip ends.
                pass
            return
        if self.path == "/slow":
            time.sleep(0.1)
        status = 503 if self.path == "/upstream-error" else 200
        status = getattr(self.server, "response_status", status)
        body = getattr(self.server, "response_body", b'{"ok":true}')
        self.send_response(status)
        for name, value in getattr(self.server, "response_headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture
def mtls_server(tmp_path):
    server_id = "spiffe://test.example/token-broker"
    client_id = "spiffe://embervm.jomcgi.dev/ns/monolith/sa/monolith"
    ca, ca_key = _ca(1)
    client_cert, client_key = _identity(
        tmp_path, "client", ca, ca_key, client_id, 3, server=False
    )
    bundle = tmp_path / "bundle.pem"
    _write_bundle(bundle, ca)
    _Handler.calls = []
    server, thread = _serve(tmp_path, server_id, ca, ca_key, 2)

    fixture = {
        "url": f"https://127.0.0.1:{server.server_port}",
        "server_id": server_id,
        "client_id": client_id,
        "cert": client_cert,
        "key": client_key,
        "bundle": bundle,
        "server": server,
        "thread": thread,
        "directory": tmp_path,
        "ca": ca,
        "ca_key": ca_key,
    }
    yield fixture

    current_server = fixture["server"]
    current_thread = fixture["thread"]
    current_server.shutdown()
    current_server.server_close()
    current_thread.join(timeout=1)


def _enable(monkeypatch, fixture) -> None:
    monkeypatch.setenv(broker_client.BROKER_SPIFFE_ENABLED_ENV, "true")
    monkeypatch.setenv(broker_client.BROKER_SPIFFE_ID_ENV, fixture["server_id"])
    monkeypatch.setenv(broker_client.BROKER_SVID_CERT_ENV, str(fixture["cert"]))
    monkeypatch.setenv(broker_client.BROKER_SVID_KEY_ENV, str(fixture["key"]))
    monkeypatch.setenv(broker_client.BROKER_SVID_BUNDLE_ENV, str(fixture["bundle"]))


def test_all_monolith_broker_legs_use_verified_mtls(monkeypatch, mtls_server):
    _enable(monkeypatch, mtls_server)
    calls = [
        ("POST", "/grants/codex-cluster/login/start"),
        ("GET", "/grants/codex-cluster/login/status"),
        ("POST", "/grants/codex-cluster/refresh"),
        ("GET", "/quota"),
    ]

    async def run() -> None:
        for method, path in calls:
            response = await broker_client.request(
                method, mtls_server["url"] + path, timeout=5
            )
            assert response.json() == {"ok": True}

    asyncio.run(run())
    assert [(method, path) for method, path, _serial in _Handler.calls] == calls


def test_wrong_server_spiffe_id_fails_before_http(monkeypatch, mtls_server):
    _enable(monkeypatch, mtls_server)
    monkeypatch.setenv(
        broker_client.BROKER_SPIFFE_ID_ENV, "spiffe://test.example/wrong-broker"
    )

    with pytest.raises(httpx.ConnectError, match="URI SAN"):
        broker_client.request_sync("GET", mtls_server["url"] + "/quota", timeout=5)

    assert _Handler.calls == []


def test_additional_server_spiffe_id_fails_before_http(monkeypatch, mtls_server):
    _enable(monkeypatch, mtls_server)
    mtls_server["server"].shutdown()
    mtls_server["server"].server_close()
    mtls_server["thread"].join(timeout=1)
    server, thread = _serve(
        mtls_server["directory"],
        mtls_server["server_id"],
        mtls_server["ca"],
        mtls_server["ca_key"],
        4,
        ("spiffe://test.example/other",),
    )
    mtls_server["url"] = f"https://127.0.0.1:{server.server_port}"
    mtls_server["server"] = server
    mtls_server["thread"] = thread

    with pytest.raises(httpx.ConnectError, match="URI SAN"):
        broker_client.request_sync("GET", mtls_server["url"] + "/quota", timeout=5)

    assert _Handler.calls == []


def test_svid_and_bundle_rotation_reload_without_stale_connection(
    monkeypatch, mtls_server
):
    _enable(monkeypatch, mtls_server)
    first = broker_client.request_sync("GET", mtls_server["url"] + "/quota", timeout=5)
    assert first.status_code == 200

    ca, ca_key = _ca(10)
    client_cert, client_key = _identity(
        mtls_server["directory"],
        "client-rotated",
        ca,
        ca_key,
        mtls_server["client_id"],
        12,
        server=False,
    )
    rotated_bundle = mtls_server["directory"] / "bundle-rotated.pem"
    _write_bundle(rotated_bundle, ca)
    mtls_server["server"].shutdown()
    mtls_server["server"].server_close()
    mtls_server["thread"].join(timeout=1)
    rotated_server, rotated_thread = _serve(
        mtls_server["directory"], mtls_server["server_id"], ca, ca_key, 11
    )
    mtls_server["url"] = f"https://127.0.0.1:{rotated_server.server_port}"
    mtls_server["server"] = rotated_server
    mtls_server["thread"] = rotated_thread
    monkeypatch.setenv(broker_client.BROKER_SVID_CERT_ENV, str(client_cert))
    monkeypatch.setenv(broker_client.BROKER_SVID_KEY_ENV, str(client_key))
    monkeypatch.setenv(broker_client.BROKER_SVID_BUNDLE_ENV, str(rotated_bundle))

    second = broker_client.request_sync("GET", mtls_server["url"] + "/quota", timeout=5)
    assert second.status_code == 200
    assert [serial for _method, _path, serial in _Handler.calls] == [3, 12]


def test_upstream_status_is_preserved(monkeypatch, mtls_server):
    _enable(monkeypatch, mtls_server)
    response = broker_client.request_sync(
        "GET", mtls_server["url"] + "/upstream-error", timeout=5
    )
    with pytest.raises(httpx.HTTPStatusError) as error:
        response.raise_for_status()
    assert error.value.response.status_code == 503


def test_transient_mixed_svid_generation_is_reloaded(monkeypatch, mtls_server):
    _enable(monkeypatch, mtls_server)
    create = broker_client.ssl.create_default_context
    calls = 0

    def transient(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ssl.SSLError("certificate rotation in progress")
        return create(*args, **kwargs)

    monkeypatch.setattr(broker_client.ssl, "create_default_context", transient)

    response = broker_client.request_sync(
        "GET", mtls_server["url"] + "/quota", timeout=5
    )

    assert response.status_code == 200
    assert calls == 2


def test_request_timeout_is_a_total_deadline(monkeypatch, mtls_server):
    _enable(monkeypatch, mtls_server)
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        broker_client.request_sync("GET", mtls_server["url"] + "/slow", timeout=0.02)
    assert time.monotonic() - started < 0.08


def test_enabled_mode_never_falls_back_to_plaintext(monkeypatch):
    monkeypatch.setenv(broker_client.BROKER_SPIFFE_ENABLED_ENV, "true")
    with pytest.raises(ValueError, match="https origin"):
        broker_client.request_sync("GET", "http://broker/quota", timeout=5)


def test_default_mode_preserves_plain_http(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *, timeout):
            assert timeout == 5

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, method, url):
            calls.append((method, url))
            return httpx.Response(
                200, json={"ok": True}, request=httpx.Request(method, url)
            )

    monkeypatch.delenv(broker_client.BROKER_SPIFFE_ENABLED_ENV, raising=False)
    monkeypatch.setattr(broker_client.httpx, "Client", Client)
    response = broker_client.request_sync("GET", "http://broker/quota", timeout=5)

    assert response.json() == {"ok": True}
    assert calls == [("GET", "http://broker/quota")]


@pytest.fixture
def dev_mtls_server(monkeypatch, mtls_server):
    _enable(monkeypatch, mtls_server)
    server_id = (
        "spiffe://embervm.jomcgi.dev/ns/embervm-dev/sa/embervm-dev-embervm-tokenbroker"
    )
    server, thread = _serve(
        mtls_server["directory"],
        server_id,
        mtls_server["ca"],
        mtls_server["ca_key"],
        4,
    )
    monkeypatch.setenv(
        broker_client.DEV_BROKER_LOGIN_URL_ENV,
        f"https://127.0.0.1:{server.server_port}",
    )
    monkeypatch.setenv(broker_client.DEV_BROKER_LOGIN_SPIFFE_ID_ENV, server_id)
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)


def test_dev_login_reuses_svid_without_changing_production_pin(
    mtls_server, dev_mtls_server
):
    async def run():
        responses = await asyncio.gather(
            broker_client.dev_login_request(
                "POST", "/grants/codex-cluster/login/start", timeout=5
            ),
            broker_client.dev_login_request(
                "GET", "/grants/codex-cluster/login/status", timeout=5
            ),
            broker_client.request("GET", mtls_server["url"] + "/quota", timeout=5),
        )
        assert all(response.json() == {"ok": True} for response in responses)

    asyncio.run(run())
    assert os.environ[broker_client.BROKER_SPIFFE_ID_ENV] == mtls_server["server_id"]
    assert sorted(_Handler.calls) == sorted(
        [
            ("POST", "/grants/codex-cluster/login/start", 3),
            ("GET", "/grants/codex-cluster/login/status", 3),
            ("GET", "/quota", 3),
        ]
    )


def test_dev_rejects_production_server_identity_before_http(
    monkeypatch, mtls_server, dev_mtls_server
):
    # A valid, trusted production server is still not the configured dev server.
    monkeypatch.setenv(broker_client.DEV_BROKER_LOGIN_URL_ENV, mtls_server["url"])
    with pytest.raises(httpx.ConnectError, match="URI SAN"):
        asyncio.run(
            broker_client.dev_login_request(
                "GET", "/grants/codex-cluster/login/status", timeout=5
            )
        )
    assert _Handler.calls == []


def test_dev_mtls_rejects_no_client_certificate(
    monkeypatch, mtls_server, dev_mtls_server
):
    # Exercise an actual TLS handshake with the client certificate omitted.
    def context_without_svid(expected_id=None):
        context = ssl.create_default_context(cafile=str(mtls_server["bundle"]))
        context.check_hostname = False
        return context, expected_id

    monkeypatch.setattr(broker_client, "_spiffe_context", context_without_svid)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(
            broker_client.dev_login_request(
                "POST", "/grants/codex-cluster/login/start", timeout=5
            )
        )
    assert _Handler.calls == []


def test_dev_missing_svid_never_falls_back(monkeypatch, mtls_server, dev_mtls_server):
    monkeypatch.setenv(
        broker_client.BROKER_SVID_CERT_ENV, str(mtls_server["directory"] / "absent.pem")
    )
    with pytest.raises(httpx.ConnectError, match="credentials unavailable"):
        asyncio.run(
            broker_client.dev_login_request(
                "GET", "/grants/codex-cluster/login/status", timeout=5
            )
        )
    assert _Handler.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/grants/codex-cluster/token"),
        ("POST", "/grants/codex-cluster/refresh"),
        ("GET", "/quota"),
        ("POST", "/grants/codex-other/login/start"),
        ("GET", "/grants/codex-cluster/login/start"),
        ("POST", "/grants/codex-cluster/login/status"),
        ("GET", "/grants/codex-cluster/login/status?token=true"),
        ("GET", "https://other.example/grants/codex-cluster/login/status"),
    ],
)
def test_dev_bridge_rejects_other_capabilities_before_transport(
    monkeypatch, method, path
):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("invalid route must not reach transport")

    monkeypatch.setattr(broker_client, "_spiffe_request", unexpected)
    with pytest.raises(ValueError, match="only codex-cluster"):
        asyncio.run(broker_client.dev_login_request(method, path, timeout=5))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, ""),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "http://broker:8080"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://user:secret@broker:8443"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://broker:8443/path"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://broker:8443?token=secret"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://broker:8443?"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://broker:8443#fragment"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://broker:8443#"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://bro\nker:8443"),
        (broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://broker:invalid"),
        (broker_client.DEV_BROKER_LOGIN_SPIFFE_ID_ENV, ""),
        (broker_client.DEV_BROKER_LOGIN_SPIFFE_ID_ENV, "spiffe://broker:8443/id"),
        (broker_client.BROKER_SPIFFE_ENABLED_ENV, "false"),
    ],
)
def test_dev_login_requires_fixed_mtls_configuration(monkeypatch, name, value):
    monkeypatch.setenv(broker_client.BROKER_SPIFFE_ENABLED_ENV, "true")
    monkeypatch.setenv(broker_client.DEV_BROKER_LOGIN_URL_ENV, "https://broker:8443")
    monkeypatch.setenv(
        broker_client.DEV_BROKER_LOGIN_SPIFFE_ID_ENV, "spiffe://test.example/dev-broker"
    )
    monkeypatch.setenv(name, value)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("invalid configuration must not reach transport")

    monkeypatch.setattr(broker_client, "_spiffe_request", unexpected)
    with pytest.raises(broker_client.DevBrokerLoginUnavailable, match="not configured"):
        asyncio.run(
            broker_client.dev_login_request(
                "GET", "/grants/codex-cluster/login/status", timeout=5
            )
        )


def test_dev_login_does_not_follow_redirects(dev_mtls_server):
    dev_mtls_server.response_status = 307
    dev_mtls_server.response_headers = {"Location": "https://other.example/token"}
    response = asyncio.run(
        broker_client.dev_login_request(
            "POST", "/grants/codex-cluster/login/start", timeout=5
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()
    assert response.status_code == 307
    assert _Handler.calls == [("POST", "/grants/codex-cluster/login/start", 3)]


def test_dev_login_bounds_response_bytes(dev_mtls_server):
    dev_mtls_server.response_body = b"x" * ((1 << 20) + 1)
    with pytest.raises(httpx.DecodingError, match="exceeded 1 MiB"):
        asyncio.run(
            broker_client.dev_login_request(
                "GET", "/grants/codex-cluster/login/status", timeout=5
            )
        )


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_dev_login_deadline_bounds_slow_streaming(dev_mtls_server, phase):
    # Each byte arrives well inside the inactivity timeout. Only an absolute
    # deadline can interrupt either HTTPResponse.readline() or read().
    dev_mtls_server.slow_stream_phase = phase
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        asyncio.run(
            broker_client.dev_login_request(
                "GET", "/grants/codex-cluster/login/status", timeout=0.15
            )
        )
    assert time.monotonic() - started < 0.9


def test_dev_login_deadline_bounds_stalled_dns_and_resolver_capacity(
    monkeypatch, mtls_server, dev_mtls_server
):
    resolve = socket.getaddrinfo
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def stalled(*args):
        if args[1] != dev_mtls_server.server_port:
            return resolve(*args)
        calls.append(args)
        try:
            assert release.wait(2), "test did not release its resolver"
            return resolve(*args)
        finally:
            finished.set()

    monkeypatch.setattr(broker_client, "_DEV_DNS_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(socket, "getaddrinfo", stalled)
    try:
        started = time.monotonic()
        with pytest.raises(httpx.TimeoutException):
            asyncio.run(
                broker_client.dev_login_request(
                    "GET", "/grants/codex-cluster/login/status", timeout=0.05
                )
            )
        assert time.monotonic() - started < 0.5
        # A second request must fail promptly, without another blocked thread.
        started = time.monotonic()
        with pytest.raises(httpx.TimeoutException):
            asyncio.run(
                broker_client.dev_login_request(
                    "GET", "/grants/codex-cluster/login/status", timeout=5
                )
            )
        assert time.monotonic() - started < 0.5
        assert len(calls) == 1
        # Dev's occupied capacity cannot make a healthy production lookup fail.
        response = broker_client.request_sync(
            "GET", mtls_server["url"] + "/quota", timeout=1
        )
        assert response.status_code == 200
    finally:
        release.set()
        assert finished.wait(1)


def test_connect_deadline_is_shared_across_resolved_addresses(monkeypatch):
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        for port in (1, 2)
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: addresses)
    attempts = []

    class UnreachableSocket:
        def __init__(self, *_args):
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            attempts.append((address, self.timeout))
            time.sleep(min(0.04, self.timeout))
            if self.timeout < 0.04:
                raise socket.timeout("connect timed out")
            raise OSError("address unreachable")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", UnreachableSocket)
    started = time.monotonic()
    with pytest.raises(socket.timeout):
        broker_client._connect_with_deadline(("broker.test", 443), started + 0.07)
    assert len(attempts) == 2
    assert attempts[0][1] > attempts[1][1]
    assert time.monotonic() - started < 0.2


def test_connect_tries_ipv4_after_unsupported_ipv6_socket(monkeypatch):
    create_socket = socket.socket
    attempted = []
    with create_socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        addresses = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args: addresses)

        def ipv4_only(family, kind, protocol):
            attempted.append(family)
            if family == socket.AF_INET6:
                raise OSError("IPv6 address family unavailable")
            return create_socket(family, kind, protocol)

        monkeypatch.setattr(socket, "socket", ipv4_only)
        connected = broker_client._connect_with_deadline(
            ("broker.test", port), time.monotonic() + 1
        )
        try:
            assert connected.getpeername() == ("127.0.0.1", port)
            assert attempted == [socket.AF_INET6, socket.AF_INET]
        finally:
            connected.close()
