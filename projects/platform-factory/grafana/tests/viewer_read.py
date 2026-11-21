"""Exercise the streamable HTTP handshake against the pinned Grafana MCP server."""

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def check():
    base = "http://127.0.0.1:8000"
    headers = {
        "Host": "mcp-grafana.grafana.svc.cluster.local:8000",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def post(message):
        data = json.dumps({"jsonrpc": "2.0", **message}).encode()
        return urlopen(Request(base + "/mcp", data, headers), timeout=15)

    init = {
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "factory-bootstrap-test", "version": "1"},
        },
    }
    for attempt in range(30):
        try:
            with post(init):
                raise AssertionError("Unauthenticated MCP initialization succeeded")
        except HTTPError as error:
            assert error.code == 401, error.code
            break
        except URLError:
            if attempt == 29:
                raise
            time.sleep(1)

    headers["Authorization"] = "Bearer " + os.environ["MCP_GRAFANA_SERVER_TOKEN"]
    with post(init) as response:
        headers["MCP-Protocol-Version"] = json.load(response)["result"][
            "protocolVersion"
        ]
    with post({"method": "notifications/initialized"}) as response:
        assert response.status == 202
    with post(
        {
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_datasources", "arguments": {}},
        }
    ) as response:
        read = json.load(response)["result"]
        assert not read.get("isError"), "Viewer datasource read failed"
        assert "Prometheus" in json.dumps(read["content"])


if __name__ == "__main__":
    check()
