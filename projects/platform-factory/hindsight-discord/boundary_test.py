"""Probe Discord REST authentication and disabled MCP in the pinned image.

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
SECRET_VALUES = {"tenant-api-key": TENANT_KEY}


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
    # order (rrf). Authentication and disabled MCP do not depend on either.
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


@pytest.mark.parametrize("path", ["/mcp/", "/mcp/discord-test/"])
@pytest.mark.parametrize("token", [None, TENANT_KEY])
def test_mcp_is_not_mounted(server, path, token):
    assert (
        request(
            server,
            "POST",
            path,
            token,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )[0]
        == 404
    )


def test_rest_recall_works_with_the_tenant_key(server):
    # Hindsight 1.2.1 answers recall on a missing bank with 404, so the
    # tenant key creates the bank first.
    status, _, body = request(
        server, "PUT", "/v1/default/banks/discord-test", TENANT_KEY, {}
    )
    assert status in (200, 201), body
    status, _, body = request(
        server,
        "POST",
        "/v1/default/banks/discord-test/memories/recall",
        TENANT_KEY,
        {
            "query": "acceptance fact",
            "tags": ["channel:test"],
            "tags_match": "all_strict",
        },
    )
    assert status == 200, body
    assert json.loads(body)["results"] == []
