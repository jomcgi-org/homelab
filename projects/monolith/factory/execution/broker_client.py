"""Token broker HTTP clients, including file-backed SPIFFE mTLS."""

from __future__ import annotations

import asyncio
import http.client
import os
import queue
import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

BROKER_SPIFFE_ENABLED_ENV = "EMBER_TOKENBROKER_SPIFFE_ENABLED"
BROKER_SPIFFE_ID_ENV = "EMBER_TOKENBROKER_SPIFFE_ID"
BROKER_SVID_CERT_ENV = "EMBER_TOKENBROKER_SVID_CERT"
BROKER_SVID_KEY_ENV = "EMBER_TOKENBROKER_SVID_KEY"
BROKER_SVID_BUNDLE_ENV = "EMBER_TOKENBROKER_SVID_BUNDLE"
DEV_BROKER_LOGIN_URL_ENV = "EMBER_DEV_TOKENBROKER_LOGIN_URL"
DEV_BROKER_LOGIN_SPIFFE_ID_ENV = "EMBER_DEV_TOKENBROKER_LOGIN_SPIFFE_ID"

_DEFAULT_SVID_DIR = Path("/run/tokenbroker-svid")
_DEFAULT_CERT = _DEFAULT_SVID_DIR / "svid.pem"
_DEFAULT_KEY = _DEFAULT_SVID_DIR / "svid_key.pem"
_DEFAULT_BUNDLE = _DEFAULT_SVID_DIR / "svid_bundle.pem"
_MAX_RESPONSE_BYTES = 1 << 20
# A stuck libc resolver must neither retain a shared executor worker beyond
# its request deadline nor create an unbounded number of resolver threads.
_PROD_DNS_SLOTS = threading.BoundedSemaphore(8)
_DEV_DNS_SLOTS = threading.BoundedSemaphore(8)
_DEV_LOGIN_ROUTES = frozenset(
    {
        ("POST", "/grants/codex-cluster/login/start"),
        ("GET", "/grants/codex-cluster/login/status"),
    }
)


class DevBrokerLoginUnavailable(RuntimeError):
    """The fixed dev login bridge has no usable mTLS configuration."""


def spiffe_enabled() -> bool:
    """Return the explicit SPIFFE client gate, rejecting ambiguous values."""
    raw = os.environ.get(BROKER_SPIFFE_ENABLED_ENV, "false").strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"{BROKER_SPIFFE_ENABLED_ENV} must be true or false")


def _required_spiffe_id(name: str = BROKER_SPIFFE_ID_ENV) -> str:
    value = os.environ.get(name, "").strip()
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
        raise ValueError(f"{name} must be an exact SPIFFE ID")
    return value


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name, str(default)).strip()
    if not raw:
        raise ValueError(f"{name} must not be empty when SPIFFE is enabled")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def _spiffe_context(expected_id: str | None = None) -> tuple[ssl.SSLContext, str]:
    if expected_id is None:
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


class _DeadlineSSLSocket(ssl.SSLSocket):
    """Apply the request deadline to every buffered HTTP receive.

    HTTPResponse reads headers and bodies through socket.makefile(). Its
    buffered reader may perform many receives for one read()/readline(), so
    setting one inactivity timeout around that outer call is insufficient.
    """

    request_deadline: float | None = None

    def recv_into(self, buffer, nbytes=None, flags=0):
        if self.request_deadline is not None:
            remaining = self.request_deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("token broker request deadline exceeded")
            self.settimeout(remaining)
        return super().recv_into(buffer, nbytes, flags)


def _deadline_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("token broker request deadline exceeded")
    return remaining


def _resolve_with_deadline(host: str, port: int, deadline: float, slots):
    _deadline_remaining(deadline)
    if not slots.acquire(blocking=False):
        raise socket.timeout("token broker DNS resolver capacity exhausted")
    result = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
        except Exception as exc:
            result.put(exc)
        finally:
            slots.release()

    # getaddrinfo has no timeout API. A bounded daemon resolver lets this
    # request terminate even if libc never returns; subsequent requests fail
    # closed when every resolver slot is occupied instead of queuing forever.
    worker = threading.Thread(target=resolve, daemon=True)
    try:
        worker.start()
    except Exception:
        slots.release()
        raise
    try:
        addresses = result.get(timeout=_deadline_remaining(deadline))
    except queue.Empty:
        raise socket.timeout("token broker DNS lookup timed out") from None
    if isinstance(addresses, Exception):
        raise addresses
    return addresses


def _connect_with_deadline(
    address, deadline: float, source_address=None, *, dns_slots=None
):
    host, port = address
    slots = _PROD_DNS_SLOTS if dns_slots is None else dns_slots
    addresses = _resolve_with_deadline(host, port, deadline, slots)
    last_error = None
    for family, kind, protocol, _canonical, target in addresses:
        remaining = _deadline_remaining(deadline)
        candidate = None
        try:
            candidate = socket.socket(family, kind, protocol)
            candidate.settimeout(remaining)
            if source_address:
                candidate.bind(source_address)
            candidate.connect(target)
            return candidate
        except OSError as exc:
            last_error = exc
            if candidate is not None:
                candidate.close()
    if last_error is not None:
        raise last_error
    raise OSError("token broker DNS returned no addresses")


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
        deadline: float,
        dns_slots,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._expected_id = expected_id
        self._deadline = deadline
        # This context is freshly loaded for this request; production and dev
        # never share it or mutate each other's peer identity/deadline.
        self._context.sslsocket_class = _DeadlineSSLSocket

        def connect_socket(address, _timeout, source_address):
            return _connect_with_deadline(
                address, deadline, source_address, dns_slots=dns_slots
            )

        self._create_connection = connect_socket

    def connect(self) -> None:
        # HTTPConnection.connect opens TCP and performs an optional CONNECT
        # tunnel, but leaves TLS to this class. Authorize the completed TLS
        # handshake before request() is allowed to write an HTTP request.
        http.client.HTTPConnection.connect(self)
        assert self.sock is not None
        self.sock.settimeout(_deadline_remaining(self._deadline))
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        self.sock.request_deadline = self._deadline
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


def _spiffe_request(
    method: str,
    url: str,
    timeout: float,
    *,
    expected_id: str | None = None,
    dns_slots=None,
) -> httpx.Response:
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
        context, expected_id = (
            _spiffe_context() if expected_id is None else _spiffe_context(expected_id)
        )
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
        deadline=deadline,
        dns_slots=_PROD_DNS_SLOTS if dns_slots is None else dns_slots,
    )
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

    def remaining() -> float:
        return _deadline_remaining(deadline)

    try:
        connection.timeout = remaining()
        connection.connect()
        assert connection.sock is not None
        connection.sock.settimeout(remaining())
        connection.request(method, target, headers={"Accept": "application/json"})
        connection.sock.settimeout(remaining())
        response = connection.getresponse()
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        remaining()
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


async def dev_login_request(
    method: str, path: str, *, timeout: float
) -> httpx.Response:
    """Call only the two dev login routes using configured, verified mTLS.

    The caller cannot choose the origin, identity, headers or credential files.
    Reuse the private tier's rotating SVID without changing the production pin.
    The raw transport follows no redirects and bounds both time and response size.
    """
    if (method, path) not in _DEV_LOGIN_ROUTES:
        raise ValueError("dev broker permits only codex-cluster login start/status")
    try:
        origin = os.environ.get(DEV_BROKER_LOGIN_URL_ENV, "").strip()
        parsed = urlsplit(origin)
        if (
            not spiffe_enabled()
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or "?" in origin
            or "#" in origin
            or any(character.isspace() for character in origin)
        ):
            raise ValueError("dev login requires a configured HTTPS origin and SVID")
        # Evaluate even before opening a connection, so malformed ports fail closed.
        _ = parsed.port
        expected_id = _required_spiffe_id(DEV_BROKER_LOGIN_SPIFFE_ID_ENV)
    except ValueError:
        raise DevBrokerLoginUnavailable(
            "Dev Codex login broker is not configured"
        ) from None
    return await asyncio.to_thread(
        _spiffe_request,
        method,
        origin.rstrip("/") + path,
        timeout,
        expected_id=expected_id,
        # An unavailable dev resolver must not consume production's capacity.
        dns_slots=_DEV_DNS_SLOTS,
    )


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
