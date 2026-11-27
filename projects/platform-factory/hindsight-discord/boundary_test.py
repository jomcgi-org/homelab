"""Probe Discord REST authentication and the MCP recall binding in the pinned image.

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
RECALL_TOKEN = "r" * 64
SECRET_VALUES = {"tenant-api-key": TENANT_KEY, "recall-token": RECALL_TOKEN}
# A bank in the recall binding (resources/hindsight.yaml MCP_BINDINGS).
BANK = "discord-1501965852042330302"
OTHER = "discord-1"


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
        elif entry["valueFrom"]["secretKeyRef"]["key"] in SECRET_VALUES:
            key = entry["valueFrom"]["secretKeyRef"]["key"]
            env[entry["name"]] = SECRET_VALUES[key]
    # The deployment's other Secrets: no database URL selects the embedded
    # Postgres, and the LLM key is never used here.
    assert "HINDSIGHT_API_DATABASE_URL" not in env
    env["HINDSIGHT_API_LLM_BASE_URL"] = "http://127.0.0.1:1/v1"
    env["HINDSIGHT_API_LLM_API_KEY"] = "unused-in-this-test"
    # The test has no network, so it embeds with the image's local model
    # instead of OpenRouter. Authentication does not depend on the model.
    for name in [n for n in env if n.startswith("HINDSIGHT_API_EMBEDDINGS_")]:
        del env[name]
    env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] = "local"
    # Nor can it reach the decision model; recall then keeps the reranker
    # order (rrf). Authentication and the MCP binding do not depend on either.
    for name in [n for n in env if n.startswith("HINDSIGHT_API_DECISION_MODEL_")]:
        del env[name]
    env["HINDSIGHT_API_DECISION_MODEL_PROVIDER"] = "none"
    # The pod mounts resources/bindings.py at the Deployment's PYTHONPATH.
    env["PYTHONPATH"] = str(ROOT / "resources")
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
    "token,status", [(None, 401), ("wrong-key", 401), (TENANT_KEY, 200)]
)
def test_rest_requires_the_tenant_key(server, token, status):
    assert request(server, "GET", "/v1/default/banks", token)[0] == status


class MCP:
    """A minimal streamable-HTTP MCP client: initialize, then JSON-RPC calls."""

    def __init__(self, base, path, token, headers=None):
        self.base, self.path, self.token, self.next_id = base, path, token, 1
        self.headers = {
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        status, headers, _ = self.post(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "hindsight-discord-boundary-test",
                    "version": "1",
                },
            },
        )
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
        text = text.strip()
        if text.startswith("{"):
            return json.loads(text)
        data = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        ]
        return json.loads(data[-1])


def refused(reply):
    # Hindsight returns a validator refusal as the recall result's error.
    return (
        "error" in reply
        or reply["result"].get("isError") is True
        or "Operation outside MCP binding" in json.dumps(reply)
    )


@pytest.fixture(scope="module")
def bank(server):
    # Hindsight 1.2.1 answers recall on a missing bank with 404.
    status, _, body = request(
        server, "PUT", f"/v1/default/banks/{BANK}", TENANT_KEY, {}
    )
    assert status in (200, 201), body
    return BANK


@pytest.mark.parametrize("token", [None, "wrong-key", TENANT_KEY])
def test_mcp_takes_only_the_recall_token(server, token):
    assert MCP(server, "/mcp/", token).status == 401


def test_recall_token_cannot_use_rest(server):
    assert request(server, "GET", "/v1/default/banks", RECALL_TOKEN)[0] == 401
    assert (
        request(server, "DELETE", f"/v1/default/banks/{BANK}", RECALL_TOKEN)[0] == 401
    )


def test_recall_token_lists_only_recall_and_recalls_a_named_bank(server, bank):
    client = MCP(server, "/mcp/", RECALL_TOKEN)
    assert client.status == 200
    tools = client.call("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["recall"]
    reply = client.call(
        "tools/call",
        {"name": "recall", "arguments": {"query": "plans", "bank_id": bank}},
    )
    assert not refused(reply), reply
    block = reply["result"]["content"][0]["text"]
    assert block.startswith("<untrusted_memory>") and block.endswith(
        "</untrusted_memory>"
    )
    payload = json.loads(
        block.removeprefix("<untrusted_memory>").removesuffix("</untrusted_memory>")
    )
    assert "results" in payload and "error" not in payload, payload


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("retain", {"content": "planted", "bank_id": BANK}),
        ("reflect", {"query": "plans", "bank_id": BANK}),
        ("delete_bank", {"bank_id": BANK}),
        ("list_banks", {}),
    ],
)
def test_recall_token_refuses_other_banks_and_tools(server, bank, name, arguments):
    reply = MCP(server, "/mcp/", RECALL_TOKEN).call(
        "tools/call", {"name": name, "arguments": arguments}
    )
    assert refused(reply), reply


@pytest.fixture(scope="module")
def other_bank(server):
    # A real bank outside the binding, so a refusal is the binding's.
    status, _, body = request(
        server, "PUT", f"/v1/default/banks/{OTHER}", TENANT_KEY, {}
    )
    assert status in (200, 201), body
    return OTHER


def test_recall_token_cannot_read_a_bank_outside_the_binding(server, bank, other_bank):
    reply = MCP(server, "/mcp/", RECALL_TOKEN).call(
        "tools/call",
        {"name": "recall", "arguments": {"query": "plans", "bank_id": other_bank}},
    )
    assert "Operation outside MCP binding" in json.dumps(reply), reply


def test_recall_without_bank_id_is_refused(server, bank):
    reply = MCP(server, "/mcp/", RECALL_TOKEN).call(
        "tools/call", {"name": "recall", "arguments": {"query": "plans"}}
    )
    assert "recall requires bank_id" in json.dumps(reply), reply


@pytest.mark.parametrize(
    "path,headers",
    [("/mcp/", {"X-Bank-Id": BANK}), (f"/mcp/{BANK}/", {})],
    ids=["bank-header", "bank-path"],
)
def test_a_session_fixed_to_a_bank_gets_no_tools(
    server, bank, other_bank, path, headers
):
    # Its recall would ignore the call's bank_id and read the session bank.
    client = MCP(server, path, RECALL_TOKEN, headers)
    assert client.call("tools/list")["result"]["tools"] == []
    for arguments in ({"query": "plans"}, {"query": "plans", "bank_id": other_bank}):
        reply = client.call("tools/call", {"name": "recall", "arguments": arguments})
        assert refused(reply), reply


def test_rest_recall_works_with_the_tenant_key(server, bank):
    status, _, body = request(
        server,
        "POST",
        f"/v1/default/banks/{bank}/memories/recall",
        TENANT_KEY,
        {
            "query": "acceptance fact",
            "tags": ["channel:test"],
            "tags_match": "all_strict",
        },
    )
    assert status == 200, body
    assert json.loads(body)["results"] == []
