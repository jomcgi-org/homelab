"""Private authenticated reader for individually shared completed tasks."""

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from urllib.parse import quote

from session_history import completed_task


def approved_messages(task, approved_hashes):
    # A revision prompt can include private review reasons. The publisher
    # receives fingerprints only and must supply independently approved prose.
    observed = {
        hashlib.sha256(
            "\n".join(p["text"] for p in msg["parts"] if "text" in p).encode()
        ).hexdigest()
        for msg in task.get("history", [])
        if msg["role"] == "ROLE_AGENT"
    }
    return sorted(observed.intersection(approved_hashes))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not hmac.compare_digest(
            self.headers.get("Authorization", ""), self.server.authorization
        ):
            return self.send_error(403)
        ref = (
            self.server.grants.get(self.path.removeprefix("/tasks/"))
            if re.fullmatch(r"/tasks/[a-f0-9]{64}", self.path)
            else None
        )
        if not ref:
            return self.send_error(404)
        # Destination, identity and share all come from the operator's grant.
        url = "http://kagent-controller.kagent.svc:8083/agents/" + "/".join(
            quote(ref[k], safe="") for k in ("namespace", "agent")
        )
        try:
            task = completed_task(
                url,
                ref["session"],
                ref["task"],
                os.environ["UPSTREAM_BEARER"],
                ref["share"],
            )
            if not task:
                return self.send_error(409)
            body = json.dumps(approved_messages(task, ref["approvedHashes"])).encode()
        except Exception:
            return self.send_error(502)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve():
    token = os.environ["READER_TOKEN"]
    if len(token) < 32:
        raise ValueError("owner-shared task reader requires a strong credential")
    with ThreadingHTTPServer(("", 8080), Handler) as server:
        server.authorization = "Bearer " + token
        server.grants = {
            hashlib.sha256(ref["task"].encode()).hexdigest(): ref
            for ref in json.loads(os.environ["OWNER_GRANTS"])
        }
        server.serve_forever()
