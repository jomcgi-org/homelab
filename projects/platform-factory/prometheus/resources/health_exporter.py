"""Export the factory's public health.json as Prometheus gauges (HOME-415).

Each scrape reads the file that public readers see, at its public URL. The
exporter calls no cluster or Cloudflare API. A failed read exports only
factory_health_read_success 0, so the rules in rules.yml use the newest
sample in a time window.
"""

import datetime
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

URL = os.environ.get("HEALTH_URL", "https://factory-data.jomcgi.dev/health.json")
HELP = {
    "read_success": "1 when the last read of health.json returned a valid document.",
    "healthy": "1 when health.json reports status healthy.",
    "generated_at_seconds": "The generatedAt time in health.json, in Unix seconds.",
    "component_up": "1 when health.json reports the component up.",
}


def read():
    request = Request(URL, headers={"User-Agent": "factory-health-exporter"})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def label(value):
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def samples(document):
    """Return (name, labels, value) for a valid health document."""
    generated = datetime.datetime.fromisoformat(
        document["generatedAt"].replace("Z", "+00:00")
    )
    if generated.tzinfo is None:
        raise ValueError("generatedAt has no time zone")
    components = [(str(c["name"]), c["state"] == "up") for c in document["components"]]
    return [
        ("read_success", "", 1),
        ("healthy", "", int(document["status"] == "healthy")),
        ("generated_at_seconds", "", int(generated.timestamp())),
    ] + [
        ("component_up", f'{{component="{label(name)}"}}', int(up))
        for name, up in components
    ]


def render(fetch):
    try:
        rows = samples(fetch())
    except Exception as error:  # Any failed read or invalid document.
        print(f"health.json read failed: {type(error).__name__}", file=sys.stderr)
        rows = [("read_success", "", 0)]
    lines = []
    for name in HELP:
        matching = [row for row in rows if row[0] == name]
        if matching:
            lines.append(f"# HELP factory_health_{name} {HELP[name]}")
            lines.append(f"# TYPE factory_health_{name} gauge")
            lines.extend(
                f"factory_health_{name}{labels} {value}"
                for _, labels, value in matching
            )
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = render(read).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
