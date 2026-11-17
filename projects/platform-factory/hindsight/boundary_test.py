"""Start the pinned Hindsight image with the deployed settings; probe its boundary.

BUILD runs this inside ghcr.io/vectorize-io/hindsight-api (exec_properties),
as the image's own user, with no network. Hindsight uses its embedded
Postgres and a dummy LLM key: authentication, tool registration and recall
need no LLM. The settings come from resources/hindsight.yaml, so a change to
the Deployment's auth or tool variables changes what this test starts.
"""

import json
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent
HINDSIGHT = Path("/app/api/.venv/bin/hindsight-api")
TENANT_KEY = "t" * 64
MCP_TOKEN = "m" * 64
SECRET_VALUES = {"tenant-api-key": TENANT_KEY, "mcp-token": MCP_TOKEN}


def deployed_env():
    deployment = next(
        d
        for d in yaml.safe_load_all((ROOT / "resources/hindsight.yaml").read_text())
        if d and d["kind"] == "Deployment"
    )
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    env = {}
    for entry in container["env"]:
        if "value" in entry:
            env[entry["name"]] = entry["value"]
        elif "configMapKeyRef" in entry["valueFrom"]:
            # The bank template's ConfigMap holds resources/<key>.
            key = entry["valueFrom"]["configMapKeyRef"]["key"]
            env[entry["name"]] = (ROOT / "resources" / key).read_text()
        elif entry["valueFrom"]["secretKeyRef"]["key"] in SECRET_VALUES:
            key = entry["valueFrom"]["secretKeyRef"]["key"]
            env[entry["name"]] = SECRET_VALUES[key]
    # The deployment's other Secrets: no database URL selects the embedded
    # Postgres, and the LLM key is never used here.
    assert "HINDSIGHT_API_DATABASE_URL" not in env
    env["HINDSIGHT_API_LLM_API_KEY"] = "unused-in-this-test"
    # The test has no network, so it embeds with the image's local model
    # instead of OpenRouter. Authentication does not depend on the model.
    for name in [n for n in env if n.startswith("HINDSIGHT_API_EMBEDDINGS_")]:
        del env[name]
    env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] = "local"
    # Nor can it reach the decision model; recall then keeps the reranker
    # order (rrf). Authentication and the MCP tools do not depend on either.
    for name in [n for n in env if n.startswith("HINDSIGHT_API_DECISION_MODEL_")]:
        del env[name]
    env["HINDSIGHT_API_DECISION_MODEL_PROVIDER"] = "none"
    return env


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(base, method, path, token=None, body=None, headers=None):
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers=all_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


class MCP:
    """A minimal streamable-HTTP MCP client: initialize, then JSON-RPC calls."""

    def __init__(self, base, path, token):
        self.base, self.path, self.token, self.next_id = base, path, token, 1
        self.headers = {"Accept": "application/json, text/event-stream"}
        status, headers, _ = self.post("initialize", initialize_params())
        self.status = status
        session = {k.lower(): v for k, v in headers.items()}.get("mcp-session-id")
        if session:
            self.headers["mcp-session-id"] = session
        if status == 200:
            self.post("notifications/initialized", None, notify=True)

    def post(self, method, params, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = self.next_id
            self.next_id += 1
        return request(self.base, "POST", self.path, self.token, body, self.headers)

    def call(self, method, params=None):
        status, _, text = self.post(method, params or {})
        assert status == 200, (status, text)
        return parse(text)


def initialize_params():
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "hindsight-boundary-test", "version": "1"},
    }


def parse(text):
    """The JSON-RPC message from a JSON or server-sent-events body."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1])


@pytest.fixture(scope="module")
def server():
    assert HINDSIGHT.exists(), (
        "runs only in the Hindsight image (BUILD exec_properties container-image)"
    )
    port = free_port()
    home = tempfile.mkdtemp(prefix="hindsight-home-")
    env = {
        **deployed_env(),
        "PATH": f"{HINDSIGHT.parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": home,
        # The image's model cache, under the image user's home.
        "HF_HOME": "/home/hindsight/.cache/huggingface",
        "HINDSIGHT_API_HOST": "127.0.0.1",
        "HINDSIGHT_API_PORT": str(port),
        "HINDSIGHT_API_SKIP_LLM_VERIFICATION": "true",
    }
    log = Path(home) / "hindsight.log"
    with log.open("w") as out:
        proc = subprocess.Popen([str(HINDSIGHT)], env=env, stdout=out, stderr=out)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 600
    try:
        while True:
            if proc.poll() is not None:
                pytest.fail(
                    f"hindsight exited {proc.returncode}:\n{log.read_text()[-4000:]}"
                )
            try:
                if request(base, "GET", "/health")[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"hindsight did not start:\n{log.read_text()[-4000:]}")
            time.sleep(2)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.parametrize(
    "token,status",
    [(None, 401), (MCP_TOKEN, 401), (TENANT_KEY, 200)],
    ids=["no-token", "mcp-token", "tenant-key"],
)
def test_rest_takes_only_the_tenant_key(server, token, status):
    assert request(server, "GET", "/v1/default/banks", token)[0] == status


def test_mcp_token_cannot_delete_through_rest(server):
    assert request(server, "DELETE", "/v1/default/banks/homelab", MCP_TOKEN)[0] == 401


@pytest.mark.parametrize("token", [None, TENANT_KEY], ids=["no-token", "tenant-key"])
def test_mcp_takes_only_the_mcp_token(server, token):
    assert MCP(server, "/mcp/homelab/", token).status == 401


@pytest.mark.parametrize(
    "path,headers",
    [("/mcp/homelab/", {}), ("/mcp/", {"X-Bank-Id": "homelab"})],
    ids=["bank-path", "multi-bank"],
)
def test_mcp_registers_only_recall_and_retain(server, path, headers):
    client = MCP(server, path, MCP_TOKEN)
    client.headers.update(headers)
    assert client.status == 200
    tools = client.call("tools/list")["result"]["tools"]
    assert sorted(tool["name"] for tool in tools) == ["recall", "retain"]
    for name in ("delete_bank", "clear_memories", "list_memories", "reflect"):
        reply = client.call("tools/call", {"name": name, "arguments": {}})
        assert "error" in reply or reply["result"].get("isError") is True, reply


def test_recall_works_with_the_mcp_token(server):
    client = MCP(server, "/mcp/homelab/", MCP_TOKEN)
    reply = client.call(
        "tools/call", {"name": "recall", "arguments": {"query": "failing checks"}}
    )
    assert "error" not in reply, reply
    assert reply["result"].get("isError") is not True, reply


def test_new_banks_get_the_schema(server):
    # Hindsight validates the template when it creates a bank (HOME-183).
    template = json.loads((ROOT / "resources/bank-template.json").read_text())
    assert request(server, "PUT", "/v1/default/banks/schema", TENANT_KEY, {})[0] == 200
    path = "/v1/default/banks/schema/config"
    status, _, text = request(server, "GET", path, TENANT_KEY)
    assert status == 200, text
    config = json.loads(text)["config"]
    assert config["retain_mission"] == template["bank"]["retain_mission"]
    assert [g["key"] for g in config["entity_labels"]] == ["app", "kind"]
    assert config["default_observation_scopes"] == [["app:*"]]
