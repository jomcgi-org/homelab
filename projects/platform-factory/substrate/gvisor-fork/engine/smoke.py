"""Check the server contract of the engine image from outside.

    python3 -I smoke.py HOST PORT BODY_BYTES [EXPECTED_FINDINGS]

Run it against a freshly started engine (one start per body: the server is
single-use). It does what the Substrate worker and a router client do:

  1. GET /healthz twice: 200 both times, and the engine still waits (the
     wakeup probe must not use up the request);
  2. POST /scan with a Content-Length body of BODY_BYTES bytes that ends with
     two fake AWS keys: 200 and the engine's stdout, which is a progress line
     "." and then the JSON. The JSON has EXPECTED_FINDINGS results (default 12:
     two keys, six rules each) and no errors;
  3. the server is gone afterwards: a new connection is refused.

Exit 0 and one line "SMOKE ok ..." on success. The body is the same for every
run of a size, so a count is comparable between runs.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import time

TAIL = b' key AKIAABCDEFGHIJKLMNOP extra AKIAZZZZZZZZZZZZZZZZ"}'
UNIT = b'{"id": 12345, "name": "value", "ok": true}, '


def body_of(size: int) -> bytes:
    head = b'{"items": ["'
    room = size - len(head) - len(TAIL)
    if room < 0:
        raise SystemExit(f"body size {size} is below the minimum {len(head) + len(TAIL)}")
    filler = (UNIT * (room // len(UNIT) + 1))[:room]
    return head + filler.replace(b'"', b"'") + TAIL


def fail(message: str) -> None:
    print(f"SMOKE fail {message}", flush=True)
    sys.exit(1)


def wait_listening(host: str, port: int, limit: float = 120.0) -> float:
    start = time.monotonic()
    while time.monotonic() - start < limit:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return time.monotonic() - start
        except OSError:
            pass
        time.sleep(0.2)
    fail(f"no 200 from /healthz in {limit:.0f}s")
    return 0.0


def main(host: str, port: int, size: int, expected: int) -> None:
    ready = wait_listening(host, port)
    for _ in range(2):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        if resp.status != 200 or text != b"ok\n":
            fail(f"/healthz answered {resp.status} {text!r}")

    body = body_of(size)
    start = time.monotonic()
    conn = http.client.HTTPConnection(host, port, timeout=120)
    conn.request("POST", "/scan", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    took = time.monotonic() - start
    if resp.status != 200:
        fail(f"/scan answered {resp.status} {raw[:200]!r}")
    # The engine prints a progress "." line before its JSON; a client skips to "{".
    start_json = raw.find(b"{")
    try:
        result = json.loads(raw[start_json:]) if start_json >= 0 else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        fail(f"/scan body is not JSON: {raw[:200]!r}")
    found = len(result.get("results", []))
    errors = result.get("errors", [])
    if found != expected or errors:
        fail(f"results={found} (want {expected}) errors={errors[:2]!r}")

    time.sleep(0.5)
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError:
        pass
    else:
        fail("the server still accepts connections after its one scan")
    print(
        f"SMOKE ok body_bytes={len(body)} findings={found} "
        f"ready_s={ready:.2f} scan_s={took:.2f} response_bytes={len(raw)}",
        flush=True,
    )


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    main(
        sys.argv[1],
        int(sys.argv[2]),
        int(sys.argv[3]),
        int(sys.argv[4]) if len(sys.argv) == 5 else 12,
    )
