"""Token broker HTTP clients, including file-backed SPIFFE mTLS."""

from __future__ import annotations

import asyncio
import http.client
import os
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

BROKER_SPIFFE_ENABLED_ENV = "EMBER_TOKENBROKER_SPIFFE_ENABLED"
BROKER_SPIFFE_ID_ENV = "EMBER_TOKENBROKER_SPIFFE_ID"
BROKER_SVID_CERT_ENV = "EMBER_TOKENBROKER_SVID_CERT"
BROKER_SVID_KEY_ENV = "EMBER_TOKENBROKER_SVID_KEY"
BROKER_SVID_BUNDLE_ENV = "EMBER_TOKENBROKER_SVID_BUNDLE"

_DEFAULT_SVID_DIR = Path("/run/tokenbroker-svid")
_DEFAULT_CERT = _DEFAULT_SVID_DIR / "svid.pem"
_DEFAULT_KEY = _DEFAULT_SVID_DIR / "svid_key.pem"
_DEFAULT_BUNDLE = _DEFAULT_SVID_DIR / "svid_bundle.pem"
_MAX_RESPONSE_BYTES = 1 << 20


def spiffe_enabled() -> bool:
    """Return the explicit SPIFFE client gate, rejecting ambiguous values."""
    raw = os.environ.get(BROKER_SPIFFE_ENABLED_ENV, "false").strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"{BROKER_SPIFFE_ENABLED_ENV} must be true or false")


def _required_spiffe_id() -> str:
    value = os.environ.get(BROKER_SPIFFE_ID_ENV, "").strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "spiffe"
        or not parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise ValueError(f"{BROKER_SPIFFE_ID_ENV} must be an exact SPIFFE ID")
    return value


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name, str(default)).strip()
    if not raw:
        raise ValueError(f"{name} must not be empty when SPIFFE is enabled")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def _spiffe_context() -> tuple[ssl.SSLContext, str]:
    expected_id = _required_spiffe_id()
    bundle = _path_from_env(BROKER_SVID_BUNDLE_ENV, _DEFAULT_BUNDLE)
    cert = _path_from_env(BROKER_SVID_CERT_ENV, _DEFAULT_CERT)
    key = _path_from_env(BROKER_SVID_KEY_ENV, _DEFAULT_KEY)
    # spiffe-helper writes the cert, key, then bundle in place. A request that
    # overlaps those three short writes can see a mixed generation. Retry only
    # that local reload window, then fail closed without reusing old material.
    for attempt in range(3):
        try:
            context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH, cafile=str(bundle)
            )
            # SPIFFE authenticates the peer by its exact URI SAN, not by a DNS
            # SAN. OpenSSL still verifies the chain before the URI check below.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))
            return context, expected_id
        except (OSError, ssl.SSLError):
            if attempt == 2:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


class _SpiffeHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that authorizes the peer before sending HTTP bytes."""

    def __init__(
        self,
        host: str,
        *,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
        expected_id: str,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._expected_id = expected_id

    def connect(self) -> None:
        # HTTPConnection.connect opens TCP and performs an optional CONNECT
        # tunnel, but leaves TLS to this class. Authorize the completed TLS
        # handshake before request() is allowed to write an HTTP request.
        http.client.HTTPConnection.connect(self)
        assert self.sock is not None
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        certificate = self.sock.getpeercert()
        uri_sans = {
            value
            for kind, value in certificate.get("subjectAltName", ())
            if kind == "URI"
        }
        if uri_sans != {self._expected_id}:
            self.close()
            raise ssl.SSLCertVerificationError(
                f"token broker presented URI SANs {sorted(uri_sans)!r}, "
                f"expected {self._expected_id!r}"
            )


def _spiffe_request(method: str, url: str, timeout: float) -> httpx.Response:
    deadline = time.monotonic() + timeout
    parsed = urlsplit(url)
    request = httpx.Request(method, url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("SPIFFE token broker URL must be an https origin")
    try:
        context, expected_id = _spiffe_context()
    except (OSError, ssl.SSLError) as exc:
        raise httpx.ConnectError(
            f"SPIFFE token broker credentials unavailable: {exc}", request=request
        ) from exc
    connection = _SpiffeHTTPSConnection(
        parsed.hostname,
        port=parsed.port or 443,
        timeout=timeout,
        context=context,
        expected_id=expected_id,
    )
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise socket.timeout("token broker request deadline exceeded")
        return value

    try:
        connection.timeout = remaining()
        connection.connect()
        assert connection.sock is not None
        connection.sock.settimeout(remaining())
        connection.request(method, target, headers={"Accept": "application/json"})
        connection.sock.settimeout(remaining())
        response = connection.getresponse()
        response_socket = connection.sock
        if response_socket is None and response.fp is not None:
            response_socket = response.fp.raw._sock
        if response_socket is not None:
            response_socket.settimeout(remaining())
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise httpx.DecodingError(
                "token broker response exceeded 1 MiB", request=request
            )
        return httpx.Response(
            response.status,
            headers=list(response.getheaders()),
            content=body,
            request=request,
        )
    except (socket.timeout, TimeoutError) as exc:
        raise httpx.TimeoutException(
            "token broker request timed out", request=request
        ) from exc
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise httpx.ConnectError(
            f"SPIFFE token broker connection failed: {exc}", request=request
        ) from exc
    finally:
        connection.close()


async def request(method: str, url: str, *, timeout: float) -> httpx.Response:
    """Issue one bounded request, reloading SVID files when SPIFFE is enabled."""
    if spiffe_enabled():
        return await asyncio.to_thread(_spiffe_request, method, url, timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method, url)


def request_sync(method: str, url: str, *, timeout: float) -> httpx.Response:
    """Synchronous counterpart used by admission paths outside an event loop."""
    if spiffe_enabled():
        return _spiffe_request(method, url, timeout)
    with httpx.Client(timeout=timeout) as client:
        return client.request(method, url)
