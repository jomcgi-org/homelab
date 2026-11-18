"""Serve only public component states. Poll independently of incoming traffic."""

import json
import sys
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
APPLICATION = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/"


def kube(path):
    request = Request(
        "https://kubernetes.default.svc" + path,
        headers={"Authorization": "Bearer " + (SERVICE_ACCOUNT / "token").read_text()},
    )
    with urlopen(
        request,
        context=ssl.create_default_context(cafile=SERVICE_ACCOUNT / "ca.crt"),
        timeout=4,
    ) as response:
        return json.load(response)


def recent(timestamp, seconds):
    observed = datetime.fromisoformat(timestamp)
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    return 0 <= age <= seconds


def cluster():
    nodes = kube("/api/v1/nodes")["items"]
    ready = nodes and all(
        any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node["status"]["conditions"]
        )
        for node in nodes
    )
    status = kube(APPLICATION + "argocd")["status"]
    return (
        ready
        and status["health"]["status"] == "Healthy"
        and recent(status["reconciledAt"], 900)
    )


def worker():
    status = kube(APPLICATION + "jomcgi-site-prod")["status"]
    return (
        status["health"]["status"] == "Healthy"
        and status["sync"]["status"] == "Synced"
        and status["operationState"]["phase"] == "Succeeded"
    )


def storyboard():
    with urlopen(
        "https://jomcgi.dev/data/factory/manifest.json", timeout=4
    ) as response:
        manifest = json.loads(response.read(4 * 1024 * 1024))
    return manifest["version"] == 1 and recent(manifest["generatedAt"], 3600)


checks = {"factory-cluster": cluster, "jomcgi-site": worker, "storyboard": storyboard}
SNAPSHOTS = Path("/snapshots")


def poll(name, check):
    while True:
        try:
            healthy = bool(check())
        except Exception:
            healthy = False
        pending = SNAPSHOTS / (name + ".tmp")
        pending.write_text(json.dumps([healthy, time.monotonic()]))
        pending.replace(SNAPSHOTS / name)
        time.sleep(20)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404)
            return
        components = {}
        for name in checks:
            try:
                ok, observed = json.loads((SNAPSHOTS / name).read_text())
            except (OSError, ValueError):
                ok, observed = False, 0
            components[name] = (
                "healthy" if ok and time.monotonic() - observed < 45 else "unhealthy"
            )
        ok = all(value == "healthy" for value in components.values())
        body = json.dumps(
            {"status": "healthy" if ok else "unhealthy", "components": components}
        ).encode()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Cache-Control",
            "private, no-store",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__" and sys.argv[1:] == ["collect"]:
    for name, check in checks.items():
        threading.Thread(target=poll, args=(name, check), daemon=True).start()
    threading.Event().wait()
elif __name__ == "__main__":
    HTTPServer(("", 8080), Health).serve_forever()
