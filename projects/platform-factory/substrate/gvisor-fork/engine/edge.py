"""Check how the engine server treats bad requests, from outside.

    python3 -I edge.py HOST PORT

Run it against a freshly started engine. Every bad request must get its error
(or a dropped connection) and leave the server waiting. At the end one scan must
still pass, and it carries header-like text inside its body, which the server
must not read as a header. Takes about 25 s: the slow clients wait for the
server's deadlines (10 s for the request head, 30 s for the body).

Exit 0 and "EDGE ok" on success.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("smoke", Path(__file__).with_name("smoke.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def status_line(host: str, port: int, data: bytes) -> bytes:
    """Send data, half-close, return the first line of the answer ('' if none)."""
    with socket.create_connection((host, port), timeout=15) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            out += chunk
    return out.split(b"\r\n")[0]


def expect(label: str, got: bytes, want: bytes) -> None:
    if got != want:
        smoke.fail(f"{label}: got {got!r}, want {want!r}")


def drip(host: str, port: int, data: bytes, gap: float, limit: float) -> float:
    """Send one byte every gap seconds. Returns the seconds until the server
    closes the connection; fails if it is still open after limit seconds."""
    start = time.monotonic()
    with socket.create_connection((host, port), timeout=5) as s:
        for byte in data:
            if time.monotonic() - start > limit:
                smoke.fail(f"the server still reads after {limit:.0f}s of dripped bytes")
            try:
                s.sendall(bytes([byte]))
                s.settimeout(gap)
                if s.recv(1) == b"":
                    return time.monotonic() - start
            except socket.timeout:
                continue
            except OSError:
                return time.monotonic() - start
    smoke.fail("the dripped request ended without the server closing it")
    return 0.0


def main(host: str, port: int) -> None:
    smoke.wait_listening(host, port)
    head = b"POST /scan HTTP/1.1\r\nHost: x\r\n"
    expect("GET /", status_line(host, port, b"GET / HTTP/1.1\r\n\r\n"), b"HTTP/1.1 405 Method Not Allowed")
    expect("no length", status_line(host, port, head + b"\r\n"), b"HTTP/1.1 400 Bad Request")
    expect("chunked", status_line(host, port, head + b"Transfer-Encoding: chunked\r\n\r\n"), b"HTTP/1.1 411 Length Required")
    for label, value in (("abc", b"abc"), ("empty", b""), ("minus", b"-5"), ("12abc", b"12abc"), ("huge", b"9" * 40)):
        got = status_line(host, port, head + b"Content-Length: " + value + b"\r\n\r\n")
        expect("Content-Length " + label, got, b"HTTP/1.1 400 Bad Request")
    got = status_line(host, port, head + b"Content-Length: %d\r\n\r\n" % (17 << 20))
    expect("17 MiB", got, b"HTTP/1.1 413 Payload Too Large")
    expect("close without data", status_line(host, port, b""), b"")
    expect("short body", status_line(host, port, head + b"Content-Length: 100\r\n\r\nabc"), b"")

    # One byte every 1.5 s must not hold the server past the head deadline.
    took = drip(host, port, head * 20, 1.5, 15)
    if not 8 <= took <= 13:
        smoke.fail(f"a slow head was dropped after {took:.1f}s, want about 10s")
    # The same for a body: the head is complete, the body drips.
    start = time.monotonic()
    with socket.create_connection((host, port), timeout=40) as s:
        s.sendall(head + b"Content-Length: 5000\r\n\r\n")
        closed = False
        while time.monotonic() - start < 40:
            try:
                s.sendall(b"x")
                s.settimeout(1.5)
                if s.recv(1) == b"":
                    closed = True
                    break
            except socket.timeout:
                continue
            except OSError:
                closed = True
                break
    took = time.monotonic() - start
    if not closed or not 27 <= took <= 33:
        smoke.fail(f"a slow body was dropped after {took:.1f}s (closed={closed}), want about 30s")

    # Header-like text inside the body is body. The head and the first body
    # bytes arrive together.
    inner = b'{"v": "\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n'
    body = inner + smoke.body_of(2048)[len(b'{"items": ["'):]
    request = head + b"Content-Length: %d\r\n\r\n" % len(body) + body
    with socket.create_connection((host, port), timeout=60) as s:
        s.sendall(request)
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
    if not raw.startswith(b"HTTP/1.1 200 OK") or raw.count(b'"check_id"') != 12:
        smoke.fail(f"the scan after the bad requests: {raw[:120]!r} findings={raw.count(b'check_id')}")
    print("EDGE ok", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], int(sys.argv[2]))
