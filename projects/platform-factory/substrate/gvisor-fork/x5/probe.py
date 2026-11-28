"""The X5 probe: the workload of each forked child in the gvisor-fork-x5 Job.

x5.py puts this file into the template as `python3 -I -c <source>`. The seed
imports what it needs, then blocks in the gVisor checkpoint before it opens
any socket (a seed must hold no endpoint at the checkpoint). Each forked child
returns from the checkpoint, binds port 80 and serves:

  GET  /healthz  200, for the wakeup probe
  POST /run      runs the three X5 checks once, answers with their JSON, and
                 exits, so the single-use child ends

The checks, from inside the child's own network namespace:

  allowed   an MCP `initialize` POST to ALLOWED_URL, which the child's
            EgressPolicy admits through the egress gateway. Any HTTP answer from
            the origin passes: the probe holds no MCP token, so 401 is expected.
  denied    a POST to DENIED_URL, which the policy does not admit. The gateway
            must refuse it: a connection error or a gateway status, never an
            answer from the origin.
  dns       resolving DNS_NAME, a public name that no rule admits. It must fail.

The probe reads nothing secret and has no credential. Its inputs are the
template's own args, which every child inherits from the seed.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import sys
import time
from urllib.parse import urlsplit

ALLOWED_URL = os.environ.get(
    "ALLOWED_URL", "http://mcp-grafana.grafana.svc.cluster.local:8000/mcp"
)
DENIED_URL = os.environ.get(
    "DENIED_URL", "http://hindsight-api.hindsight.svc.cluster.local:8888/"
)
DNS_NAME = os.environ.get("DNS_NAME", "example.com")
TIMEOUT = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "10"))
CHECKPOINT = os.environ.get("PROBE_CHECKPOINT", "1") != "0"

INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "gvisor-fork-x5", "version": "1"},
        },
    }
).encode()


def checkpoint() -> None:
    """Block in the gVisor checkpoint; each forked child returns from it."""
    with open("/proc/gvisor/checkpoint", "r+b", buffering=0) as f:
        f.write(b"1")
        result = f.read(64)
    if result != b"restore\n":
        sys.exit(f"probe: unexpected checkpoint result {result!r}")


def post(url: str, body: bytes) -> dict:
    parts = urlsplit(url)
    start = time.monotonic()
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=TIMEOUT)
    try:
        conn.request(
            "POST",
            parts.path or "/",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        response = conn.getresponse()
        payload = response.read(512)
        return {
            "status": response.status,
            "server": response.getheader("server") or "",
            "body": payload.decode("utf-8", "replace")[:200],
            "ms": round((time.monotonic() - start) * 1000, 1),
        }
    except OSError as err:
        return {
            "error": f"{type(err).__name__}: {err}"[:200],
            "ms": round((time.monotonic() - start) * 1000, 1),
        }
    finally:
        conn.close()


def resolve(name: str) -> dict:
    start = time.monotonic()
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(name, 443)})
        return {"addresses": addrs, "ms": round((time.monotonic() - start) * 1000, 1)}
    except OSError as err:
        return {
            "error": f"{type(err).__name__}: {err}"[:200],
            "ms": round((time.monotonic() - start) * 1000, 1),
        }


def run_checks() -> dict:
    return {
        "allowed": post(ALLOWED_URL, INITIALIZE),
        "denied": post(DENIED_URL, INITIALIZE),
        "dns": resolve(DNS_NAME),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def answer(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.answer(200, b"{}")
        else:
            self.answer(404, b"{}")

    def do_POST(self) -> None:
        if self.path != "/run":
            self.answer(404, b"{}")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 1 << 16))
        self.answer(200, json.dumps(run_checks()).encode())
        self.wfile.flush()
        # Single use: the child ends after one run.
        os._exit(0)


def main() -> None:
    if CHECKPOINT:
        checkpoint()
    server = http.server.HTTPServer(("0.0.0.0", 80), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
