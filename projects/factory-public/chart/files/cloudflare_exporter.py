"""Export the egress IP and the factory's Cloudflare credential checks (HOME-634).

Every factory Cloudflare API token and R2 key accepts calls only from the home
connection's IPv4 address. A loop runs every INTERVAL_SECONDS (default 60):

- TRACE_URL: read the address that Cloudflare sees, from the ip= line of a
  /cdn-cgi/trace page.
- API_CREDENTIAL: GET API_URL with the token in CLOUDFLARE_API_TOKEN.

Each pod sets only the parts its namespace holds. The exporter checks API
tokens only: only the mirror CronJobs hold the R2 mirror key, so no exporter
pod tests an R2 key. A 2xx answer accepts a credential and a 401 or 403
refuses it. Any other result (another status, no answer or no credential) sets
check_success to 0 and keeps the last accept or refuse, so a network gap does
not change the alerts in rules.yml. A failed trace read keeps the last address
in the same way.

The egress IP is private. The exporter gives it only to Prometheus and never
logs it, because pod logs can go to Honeycomb. Logs name a check, the HTTP
status and the error type, never an address, a body or a credential.
"""

import ipaddress
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

AGENT = "factory-cloudflare-exporter"
HELP = {
    "factory_egress_ip_info": "The egress IP that Cloudflare saw at the last valid trace read, in label ip.",
    "factory_egress_ip_observed_timestamp_seconds": "Unix time of the last valid trace read, with the address in label ip.",
    "factory_egress_ip_read_success": "1 when the last trace read returned an IP address.",
    "factory_cloudflare_credential_auth_success": "1 when Cloudflare accepted the credential at its last answer, 0 when it refused it with 401 or 403.",
    "factory_cloudflare_credential_check_success": "1 when the last check got an answer that accepts or refuses the credential.",
}


class NoCredential(Exception):
    """The pod's environment has no value for the credential."""


def fetch(request):
    """Return the HTTP status and the start of the body. No answer raises OSError."""
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.read(4096)
    except HTTPError as error:
        return error.code, b""


def parse_trace(text):
    # The IP filters hold the IPv4 egress, and the pods have IPv4 only. An
    # IPv6 answer is not the address to allow, so it counts as a failed read.
    for line in text.splitlines():
        if line.startswith("ip="):
            return str(ipaddress.IPv4Address(line[3:].strip()))
    raise ValueError("no ip= line")


def read_trace(url):
    status, body = fetch(Request(url, headers={"User-Agent": AGENT}))
    if status != 200:
        raise ValueError(f"HTTP {status}")
    return parse_trace(body.decode())


def api_status(url, token):
    if not token:
        raise NoCredential
    headers = {"Authorization": f"Bearer {token}", "User-Agent": AGENT}
    return fetch(Request(url, headers=headers))[0]


def outcome(status):
    """1 for an accepted credential, 0 for a refused one, None for neither."""
    if 200 <= status < 300:
        return 1
    if status in (401, 403):
        return 0
    return None


# The results of the last loop. LOCK keeps a scrape from reading a half-done
# update.
LOCK = threading.Lock()
egress = {}  # The last read result, valid address and its observation time.
credentials = {}  # name: {"ok": 0 or 1, "auth": 0, 1 or None}
reasons = {}  # check name: the last logged result


def note(check, reason):
    """Log a check's result when it changes."""
    if reasons.get(check) != reason:
        reasons[check] = reason
        print(f"{check}: {reason}", file=sys.stderr)


def update(trace, checks):
    """Run the trace read (a callable or None) and each check once.

    checks maps a credential name to a callable that returns an HTTP status.
    """
    if trace is not None:
        try:
            ip, reason = trace(), "read"
        except Exception as error:  # Any failed read or invalid page.
            ip, reason = None, f"read failed: {type(error).__name__}"
        note("trace", reason)
        with LOCK:
            egress["ok"] = int(ip is not None)
            if ip is not None:
                egress["ip"] = ip
                egress["observed_at"] = int(time.time())
    for name, check in checks.items():
        try:
            status = check()
            auth = outcome(status)
            reason = {1: "accepted", 0: "refused"}.get(auth, "answered")
            reason = f"{reason} with HTTP {status}"
        except Exception as error:  # No answer or no credential.
            auth, reason = None, f"check failed: {type(error).__name__}"
        note(name, reason)
        with LOCK:
            last = credentials.get(name, {}).get("auth")
            credentials[name] = {
                "ok": int(auth is not None),
                "auth": last if auth is None else auth,
            }


def label(value):
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def render():
    rows = []
    with LOCK:
        if "ok" in egress:
            rows.append(("factory_egress_ip_read_success", "", egress["ok"]))
        if "ip" in egress:
            labels = f'{{ip="{label(egress["ip"])}"}}'
            rows.append(("factory_egress_ip_info", labels, 1))
            rows.append(
                (
                    "factory_egress_ip_observed_timestamp_seconds",
                    labels,
                    egress["observed_at"],
                )
            )
        for name in sorted(credentials):
            result = credentials[name]
            labels = f'{{credential="{label(name)}"}}'
            rows.append(
                ("factory_cloudflare_credential_check_success", labels, result["ok"])
            )
            if result["auth"] is not None:
                rows.append(
                    (
                        "factory_cloudflare_credential_auth_success",
                        labels,
                        result["auth"],
                    )
                )
    lines = []
    for name, text in HELP.items():
        matching = [row for row in rows if row[0] == name]
        if matching:
            lines.append(f"# HELP {name} {text}")
            lines.append(f"# TYPE {name} gauge")
            lines.extend(f"{name}{labels} {value}" for _, labels, value in matching)
    return "\n".join(lines) + "\n"


def from_env(env):
    """Return the trace read and the checks that the environment configures."""

    def trace():
        return read_trace(env["TRACE_URL"])

    checks = {}
    if env.get("API_CREDENTIAL"):
        checks[env["API_CREDENTIAL"]] = lambda: api_status(
            env["API_URL"], env.get("CLOUDFLARE_API_TOKEN", "")
        )
    return (trace if env.get("TRACE_URL") else None), checks


def loop(trace, checks, interval):
    while True:
        update(trace, checks)
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    trace, checks = from_env(os.environ)
    interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
    threading.Thread(target=loop, args=(trace, checks, interval), daemon=True).start()
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
