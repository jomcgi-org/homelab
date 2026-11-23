"""Check the Pro engine image from outside, with the placeholder token.

    python3 -I smoke-pro.py HOST PORT planted|clean|edge [BODY_BYTES [MIN_FINDINGS]]

Run it against a freshly started engine (one start per run: the server is single-use).
It uses the client code of ../engine/smoke.py and ../engine/edge.py, the same server
contract as the OSS image. What is new is the engine behind it:

  planted  POST /scan with a body that holds 11 fake secrets: the engine must answer
           with at least MIN_FINDINGS results (default 5) and no errors. This proves
           that the engine starts and scans with the placeholder token and that the
           rules loaded. A token the engine does not accept gives no server at all.
  clean    the same filler without the secrets: exactly 0 results and no errors.
  edge     bad requests (bad length, chunked, too big, short body, header-like text in
           the body), then one planted scan. No slow-client waits: the server code is
           the OSS shim, which edge.py tests for 25 s in the OSS workflow.

Exit 0 and one line "SMOKE-PRO ok ..." on success; the line holds the findings count
and the distinct rule ids, for the job summary. The secrets are made at run time from
a fixed random seed: the source holds no complete token, so no scanner flags it.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import random
import socket
import string
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "engine"
DEFAULT_MIN_FINDINGS = 5
HEAD = b'{"items": ["'
UNIT = b"{'id': 12345, 'name': 'value', 'ok': true}, "


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ENGINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_secrets() -> bytes:
    """Eleven secret-shaped lines, random but fixed (seed 7), as in the spike that
    measured the Pro engine: a GitHub token, an AWS key pair, Slack, Stripe, SendGrid,
    npm, OpenAI-style, GitLab, a database URI with a password, a PEM private key.
    Every prefix is joined to its random part here, so no source file holds a token."""
    r = random.Random(7)
    alnum = string.ascii_letters + string.digits

    def al(n: int, chars: str = alnum) -> str:
        return "".join(r.choice(chars) for _ in range(n))

    lines = [
        'github_token = "ghp' + "_" + al(36) + '"',
        'aws_access_key_id = "AK'
        + "IA"
        + al(16, string.ascii_uppercase + string.digits)
        + '"',
        'aws_secret_access_key = "' + al(40, alnum + "+/") + '"',
        'slack = "xox'
        + "b-"
        + al(12, string.digits)
        + "-"
        + al(12, string.digits)
        + "-"
        + al(24)
        + '"',
        'stripe = "sk_' + "live_" + al(24) + '"',
        'sendgrid = "SG'
        + "."
        + al(22, alnum + "-_")
        + "."
        + al(43, alnum + "-_")
        + '"',
        'npm = "npm' + "_" + al(36) + '"',
        'openai = "sk-' + "proj-" + al(64, alnum + "-_") + '"',
        'gitlab = "gl' + "pat-" + al(20, alnum + "-_") + '"',
        'db = "postgres://admin:' + al(16) + '@db.internal.example.com:5432/app"',
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\n"
        + "\n".join(al(64, alnum + "+/") for _ in range(12))
        + "\n-----END RSA "
        + "PRIVATE KEY-----",
    ]
    return ("\n".join(lines) + "\n").encode()


def filler(size: int) -> bytes:
    room = size - len(HEAD)
    return HEAD + (UNIT * (room // len(UNIT) + 1))[:room]


def body_of(size: int, planted: bool) -> bytes:
    """A body of exactly size bytes; with the secrets in its middle when planted."""
    body = filler(size)
    if not planted:
        return body
    secrets = fake_secrets()
    if size < len(secrets) + len(HEAD) + 2:
        raise SystemExit(f"body size {size} is too small for the planted secrets")
    middle = (size - len(secrets)) // 2
    return body[:middle] + b"\n" + secrets + body[middle + 1 : size - len(secrets)]


def scan(smoke, host: str, port: int, body: bytes):
    conn = http.client.HTTPConnection(host, port, timeout=180)
    conn.request(
        "POST", "/scan", body=body, headers={"Content-Type": "application/json"}
    )
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    if resp.status != 200:
        smoke.fail(f"/scan answered {resp.status} {raw[:200]!r}")
    start = raw.find(b"{")
    try:
        result = json.loads(raw[start:]) if start >= 0 else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        smoke.fail(f"/scan body is not JSON: {raw[:200]!r}")
    return result, len(raw)


def check_result(smoke, result: dict, planted: bool, minimum: int) -> str:
    found = result.get("results", [])
    errors = result.get("errors", [])
    ids = sorted({r.get("check_id", "?").rsplit(".", 1)[-1] for r in found})
    if errors:
        smoke.fail(f"errors={errors[:2]!r}")
    if planted and len(found) < minimum:
        smoke.fail(f"results={len(found)}, want at least {minimum} on the planted body")
    if not planted and found:
        smoke.fail(f"results={len(found)} on a clean body, want 0: {ids[:5]!r}")
    return f"findings={len(found)} rules={len(ids)} ids={','.join(ids[:12])}"


def healthz_twice(smoke, host: str, port: int) -> float:
    ready = smoke.wait_listening(host, port, limit=180.0)
    for _ in range(2):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        if resp.status != 200 or text != b"ok\n":
            smoke.fail(f"/healthz answered {resp.status} {text!r}")
    return ready


def server_is_gone(smoke, host: str, port: int) -> None:
    time.sleep(0.5)
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError:
        return
    smoke.fail("the server still accepts connections after its one scan")


def run_scan_mode(host: str, port: int, mode: str, size: int, minimum: int) -> None:
    smoke = load("smoke")
    ready = healthz_twice(smoke, host, port)
    planted = mode == "planted"
    body = body_of(size, planted)
    start = time.monotonic()
    result, raw_bytes = scan(smoke, host, port, body)
    took = time.monotonic() - start
    summary = check_result(smoke, result, planted, minimum)
    server_is_gone(smoke, host, port)
    print(
        f"SMOKE-PRO ok mode={mode} body_bytes={len(body)} {summary} "
        f"ready_s={ready:.2f} scan_s={took:.2f} response_bytes={raw_bytes}",
        flush=True,
    )


def run_edge(host: str, port: int, minimum: int) -> None:
    smoke = load("smoke")
    edge = load("edge")
    smoke.wait_listening(host, port, limit=180.0)
    head = b"POST /scan HTTP/1.1\r\nHost: x\r\n"
    edge.expect(
        "GET /",
        edge.status_line(host, port, b"GET / HTTP/1.1\r\n\r\n"),
        b"HTTP/1.1 405 Method Not Allowed",
    )
    edge.expect(
        "no length",
        edge.status_line(host, port, head + b"\r\n"),
        b"HTTP/1.1 400 Bad Request",
    )
    edge.expect(
        "chunked",
        edge.status_line(host, port, head + b"Transfer-Encoding: chunked\r\n\r\n"),
        b"HTTP/1.1 411 Length Required",
    )
    for label, value in (
        ("abc", b"abc"),
        ("empty", b""),
        ("minus", b"-5"),
        ("huge", b"9" * 40),
    ):
        got = edge.status_line(
            host, port, head + b"Content-Length: " + value + b"\r\n\r\n"
        )
        edge.expect("Content-Length " + label, got, b"HTTP/1.1 400 Bad Request")
    got = edge.status_line(
        host, port, head + b"Content-Length: %d\r\n\r\n" % (17 << 20)
    )
    edge.expect("17 MiB", got, b"HTTP/1.1 413 Payload Too Large")
    edge.expect("close without data", edge.status_line(host, port, b""), b"")
    edge.expect(
        "short body",
        edge.status_line(host, port, head + b"Content-Length: 100\r\n\r\nabc"),
        b"",
    )
    # Header-like text inside the body is body. The head and the first body bytes
    # arrive together.
    inner = b"\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n"
    body = body_of(16384, True)
    body = body[:2000] + inner + body[2000 + len(inner) :]
    request = head + b"Content-Length: %d\r\n\r\n" % len(body) + body
    with socket.create_connection((host, port), timeout=180) as s:
        s.sendall(request)
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
    if not raw.startswith(b"HTTP/1.1 200 OK"):
        smoke.fail(f"the scan after the bad requests: {raw[:120]!r}")
    start = raw.find(b"{")
    result = json.loads(raw[start:]) if start >= 0 else {}
    summary = check_result(smoke, result, True, minimum)
    print(f"EDGE-PRO ok {summary}", flush=True)


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[2] not in ("planted", "clean", "edge") or len(argv) > 6:
        sys.exit(__doc__)
    host, port, mode = argv[0], int(argv[1]), argv[2]
    size = int(argv[3]) if len(argv) > 3 else 16384
    minimum = int(argv[4]) if len(argv) > 4 else DEFAULT_MIN_FINDINGS
    if mode == "edge":
        run_edge(host, port, minimum)
    else:
        run_scan_mode(host, port, mode, size, minimum)


if __name__ == "__main__":
    main(sys.argv[1:])
