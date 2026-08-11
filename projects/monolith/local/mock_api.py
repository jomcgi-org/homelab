#!/usr/bin/env python3
"""Small, stateful API fixture for exploring the monolith frontend locally.

It deliberately uses only the Python standard library so the local deployment
does not need the production Python environment or a running Postgres cluster.
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).parent
DEFAULT_DB = ROOT / ".data" / "mock.sqlite3"


def now():
    return datetime.now(timezone.utc).isoformat()


def seed(db):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
          note_id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
          due_date TEXT NOT NULL, area TEXT NOT NULL
        );
        """
    )
    if not db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
        db.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, date('now'), ?)",
            [
                ("local-task-1", "Review the local deployment", "todo", "platform"),
                ("local-task-2", "Capture a screenshot of the dashboard", "in-progress", "design"),
                ("local-task-3", "Try the notes graph interactions", "todo", "research"),
            ],
        )
        db.commit()


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/healthz", "/api/health"):
            return self.send_json({"status": "ok", "mode": "local"})
        if path == "/api/home/observability/stats":
            return self.send_json({
                "cluster": {"nodes": 1, "pods": 6, "cpu_used_cores": 1.8, "cpu_capacity_cores": 8, "memory_used_gb": 3.2, "memory_capacity_gb": 16, "argocd_apps": 4},
                "gpu": {"utilization_pct": 12, "memory_used_gb": 2, "memory_total_gb": 24},
                "knowledge": {"facts": 1284},
                "deploy": {"latest_commit_sha": "local", "deployed_at": now()},
            })
        if path == "/api/home/dashboard":
            return self.send_json({
                "health": {"healthy": True, "scanned": 6, "unhealthy": {}},
                "alerts": {"firing": []},
                "today": {"events": [{"title": "Local deployment", "time": "09:00", "endTime": "17:00", "allDay": False}]},
                "github": {"prs": [], "issues": []},
            })
        if path in ("/api/knowledge/tasks/daily", "/api/knowledge/tasks/weekly"):
            with sqlite3.connect(self.db_path) as db:
                rows = db.execute("SELECT note_id, title, status, due_date, area FROM tasks ORDER BY note_id").fetchall()
            return self.send_json({"tasks": [dict(zip(("note_id", "title", "status", "due_date", "area"), row)) for row in rows]})
        if path == "/api/knowledge/graph":
            return self.send_json({"nodes": [{"id": "local", "title": "Local deployment", "type": "project"}], "edges": []})
        return self.send_json({"detail": "mock endpoint not implemented", "path": path}, 404)

    def do_PATCH(self):  # noqa: N802
        path = urlparse(self.path).path
        prefix = "/api/knowledge/tasks/"
        if not path.startswith(prefix):
            return self.send_json({"detail": "not found"}, 404)
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE tasks SET status = ? WHERE note_id = ?", (data.get("status", "todo"), path[len(prefix):]))
            db.commit()
        return self.send_json({"status": data.get("status", "todo")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as db:
        seed(db)
    Handler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock API listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
