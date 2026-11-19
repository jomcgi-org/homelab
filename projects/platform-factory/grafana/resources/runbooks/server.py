"""Serve reviewed runbooks using the Prometheus read API."""

import json
import math
import re
import socket
import time
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

BOOKS = {
    p.stem: json.loads(p.read_text()) for p in Path(__file__).parent.glob("*.json")
}
API = "http://prometheus.prometheus.svc:9090/api/v1/"


def bind(expr, params):
    def matcher(match):
        values = params.get(match[1])
        if not values:
            return '=~".*"'
        return "=" + json.dumps(values[0], ensure_ascii=False)

    return re.sub(r"=~@(\w+)", matcher, expr)


def query(expr, history=False):
    params = {"query": expr}
    endpoint = "query"
    if history:
        now = int(time.time())
        endpoint = "query_range"
        params.update(start=now - 3600, end=now, step=60)
    with urlopen(API + endpoint + "?" + urlencode(params), timeout=4) as response:
        return json.load(response)["data"]["result"]


def labels(series):
    return escape(
        ", ".join(f"{k}={v}" for k, v in series["metric"].items() if k != "__name__")
    )


def graph(expr):
    parts = []
    for series in query(expr, history=True):
        points = [
            (int(t), float(v)) for t, v in series["values"] if math.isfinite(float(v))
        ]
        if not points:
            continue
        ts, ys = zip(*points)
        coords = " ".join(
            f"{(t - min(ts)) * 300 / max(1, max(ts) - min(ts)):.1f},{54 - (v - min(ys)) * 50 / (max(ys) - min(ys) or 1):.1f}"
            for t, v in points
        )
        parts.append(
            f'<p>{labels(series)} · {min(ys):g} to {max(ys):g}</p><svg viewBox="0 0 300 60"><polyline points="{coords}" fill="none" stroke="currentColor"/></svg>'
        )
    return "".join(parts) or "No samples"


def check(book, expr):
    rows = query(expr)
    passed = all(
        float(row["value"][1]) < book["limit"]
        if book["condition"] == "lt"
        else float(row["value"][1]) >= book["limit"]
        for row in rows
    )
    result = ("Pass" if passed else "Fail") if rows else "Unknown · no samples"
    return result + "".join(
        f"<p>{labels(row)} {escape(row['value'][1])}</p>"
        for row in rows
        if "empty_label" not in book or row["metric"].get(book["empty_label"])
    )


def quota(details):
    usage = query(details["usage"])
    resets = {
        (r["metric"]["grant"], r["metric"]["window"]): datetime.fromtimestamp(
            float(r["value"][1]), UTC
        ).isoformat()
        for r in query(details["resets"])
    }
    return (
        "".join(
            f"<p>{labels(row)} · {escape(row['value'][1])}% · Reset: {resets.get((row['metric']['grant'], row['metric']['window']), 'unreported')}</p>"
            for row in usage
        )
        or "No usage reported"
    )


def section(title, render, *args):
    try:
        body = render(*args)
    except (OSError, ValueError, KeyError, OverflowError):
        body = "Live data unavailable"
    return f"<h2>{title}</h2>{body}"


# A reviewed action ID selects its preview route and service. Book content and
# alert query parameters cannot supply a destination or command.
ACTIONS = {
    "kagent-revert": (
        "runbook-kagent-revert.grafana.svc",
        "/actions/kagent-revert",
        "Review locked sessions",
    ),
}


def action_buttons(book):
    forms = []
    for action_id in book.get("actions", []):
        service, route, label = ACTIONS[action_id]
        if service_available(service):
            forms.append(
                f'<form method="get" action="{route}"><button>{label}</button></form>'
            )
    return "".join(forms)


def service_available(service):
    try:
        socket.getaddrinfo(service, 8080)
    except OSError:
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        name = url.path.removeprefix("/")
        if not name:
            name = "Factory runbooks"
            content = "".join(
                f'<p><a href="/{key}">{key}</a></p>' for key in sorted(BOOKS)
            )
        elif name in BOOKS:
            book = BOOKS[name]
            params = parse_qs(url.query)
            content = section("Current check", check, book, bind(book["check"], params))
            content += action_buttons(book)
            if "details" in book:
                content += section("Codex grant windows", quota, book["details"])
            content += section("Past hour", graph, bind(book["graph"], params))
            content += "<h2>Operator steps</h2><pre>" + escape(book["steps"]) + "</pre>"
        else:
            self.send_error(404)
            return
        body = (
            f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{name}</title>'
            "<style>body{font:1rem/1.5 system-ui;margin:auto;padding:1rem;max-width:52rem;overflow-wrap:anywhere}pre{white-space:pre-wrap}svg{max-width:32rem;width:100%}</style>"
            f"<h1>{name}</h1>{content}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
