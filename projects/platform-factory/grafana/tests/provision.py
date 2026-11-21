"""Run the production bootstrap against fresh Grafana with a Secret API stub."""

import base64
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, "/resources")
import provision_viewer

fixture = Path("/fixture")


def request(req, **kwargs):
    if not req.full_url.startswith("https://kubernetes.default.svc/"):
        return urlopen(req, **kwargs)
    assert req.get_header("Authorization") == "Bearer test-identity"
    if req.get_method() == "GET":
        return io.BytesIO(b'{"metadata":{"resourceVersion":"1"}}')
    assert req.get_method() == "PATCH"
    body = json.loads(req.data)
    if req.full_url.endswith("/mcp-grafana-viewer"):
        (fixture / "viewer").write_bytes(base64.b64decode(body["data"]["token"]))
    else:
        assert req.full_url.endswith("/mcp-grafana-caller")
        token = body["stringData"]["caller"]
        assert token
        (fixture / "caller.env").write_text(f"MCP_GRAFANA_SERVER_TOKEN={token}\n")
    return io.BytesIO(b"{}")


for _ in range(90):
    try:
        with urlopen("http://127.0.0.1:3000/api/health", timeout=2):
            break
    except URLError:
        time.sleep(1)

Path("/run/kube").mkdir()
Path("/run/kube/token").write_text("test-identity")
with (
    patch.object(provision_viewer, "urlopen", request),
    patch.object(provision_viewer.ssl, "create_default_context", return_value=None),
):
    provision_viewer.main()
