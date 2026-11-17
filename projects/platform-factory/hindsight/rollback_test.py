"""Prove the way back from 1,024-dimensional remote embeddings to the local model.

HOME-176 moves factory Hindsight from the image's local bge-small-en-v1.5 (384
dimensions) to Voyage 4 Large through OpenRouter (1,024). After the first
retain, Hindsight refuses to start with the local model, so a manifest revert
alone cannot restore service. This test runs the recovery that
projects/factory/ARCHITECTURE.md "Memory" describes, inside the deployed image
with no network:

1. Start Hindsight on an OpenAI-compatible stub that returns 1,024-dimensional
   vectors. The openrouter provider uses the same client class.
2. Retain memories without an LLM (chunks mode), then stop the server.
3. Run `hindsight-admin reembed --all-schemas` with the local model's settings.
4. Start Hindsight on the local model, as the reverted Deployment would, and
   recall a retained memory.
5. Run `hindsight-admin reembed --finish`.
"""

import hashlib
import json
import math
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BIN = Path("/app/api/.venv/bin")
REMOTE_DIMENSIONS = 1024
BANK = "rollback"
MEMORIES = [
    "To recover a frozen agent session, delete the worker pod and revert the session.",
    "Factory nodes run zram swap with GOGC set to 50 for Go services.",
    "The merge queue cannot rebase a branch that carries a merge commit.",
]


def vector(text):
    """A deterministic unit vector, so the same text always embeds the same way."""
    out, counter = [], 0
    while len(out) < REMOTE_DIMENSIONS:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        out += [b / 127.5 - 1 for b in struct.unpack("32B", digest)]
        counter += 1
    out = out[:REMOTE_DIMENSIONS]
    norm = math.sqrt(sum(x * x for x in out))
    return [x / norm for x in out]


class Stub(BaseHTTPRequestHandler):
    """POST /v1/embeddings in the OpenAI shape, with float vectors."""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        reply = json.dumps(
            {
                "object": "list",
                "model": body.get("model", "stub"),
                "data": [
                    {"object": "embedding", "index": i, "embedding": vector(str(t))}
                    for i, t in enumerate(texts)
                ],
                "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(base, method, path, body=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class Server:
    """One Hindsight API process; all phases share HOME, so the embedded Postgres."""

    def __init__(self, env, log):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = log
        env = {
            **env,
            "HINDSIGHT_API_HOST": "127.0.0.1",
            "HINDSIGHT_API_PORT": str(self.port),
        }
        with log.open("a") as out:
            self.proc = subprocess.Popen(
                [str(BIN / "hindsight-api")], env=env, stdout=out, stderr=out
            )
        deadline = time.monotonic() + 600
        while True:
            if self.proc.poll() is not None:
                pytest.fail(f"hindsight exited {self.proc.returncode}:\n{self.tail()}")
            try:
                if request(self.base, "GET", "/health")[0] == 200:
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                self.stop()
                pytest.fail(f"hindsight did not start:\n{self.tail()}")
            time.sleep(2)

    def tail(self):
        return self.log.read_text()[-4000:]

    def recalls(self, query, memory):
        """Whether recall for ``query`` returns ``memory``; chunk text may differ."""
        path = f"/v1/default/banks/{BANK}/memories/recall"
        status, body = request(self.base, "POST", path, {"query": query})
        assert status == 200, (status, body, self.tail())
        return any(memory[:60] in r["text"] for r in body["results"])

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def admin(env, log, *args):
    with log.open("a") as out:
        out.write(f"\n$ hindsight-admin {' '.join(args)}\n")
        out.flush()
        done = subprocess.run(
            [str(BIN / "hindsight-admin"), *args],
            env=env,
            stdout=out,
            stderr=out,
            timeout=600,
        )
    assert done.returncode == 0, log.read_text()[-4000:]


def test_reembed_restores_the_local_model_after_remote_embeddings():
    for name in ("hindsight-api", "hindsight-admin"):
        assert (BIN / name).exists(), (
            f"{name}: runs only in the Hindsight image (BUILD exec_properties)"
        )
    home = tempfile.mkdtemp(prefix="hindsight-rollback-")
    log = Path(home) / "hindsight.log"
    base_env = {
        "PATH": f"{BIN}:/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": home,
        # The image's model cache, under the image user's home.
        "HF_HOME": "/home/hindsight/.cache/huggingface",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        # No LLM: retain stores chunks, and nothing here needs extraction.
        "HINDSIGHT_API_LLM_PROVIDER": "none",
    }
    stub = ThreadingHTTPServer(("127.0.0.1", free_port()), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_url = f"http://127.0.0.1:{stub.server_port}/v1"
    remote_env = {
        **base_env,
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": stub_url,
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "stub",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "stub-1024",
    }
    # What the Deployment ran before HOME-176, and what a revert restores.
    local_env = {**base_env, "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "local"}
    try:
        remote = Server(remote_env, log)
        try:
            bank = request(remote.base, "PUT", f"/v1/default/banks/{BANK}", {})
            assert bank[0] in (200, 201), (bank, remote.tail())
            status, body = request(
                remote.base,
                "POST",
                f"/v1/default/banks/{BANK}/memories",
                {"items": [{"content": m} for m in MEMORIES], "async": False},
            )
            assert status == 200, (status, body, remote.tail())
            assert remote.recalls(MEMORIES[0], MEMORIES[0]), remote.tail()
        finally:
            remote.stop()

        admin(local_env, log, "reembed", "--all-schemas")

        local = Server(local_env, log)
        try:
            query = "How do I recover a frozen agent session?"
            assert local.recalls(query, MEMORIES[0]), local.tail()
        finally:
            local.stop()
        admin(local_env, log, "reembed", "--all-schemas", "--finish")
    finally:
        stub.shutdown()
