"""Exercise the exact packaged Codex producer against a local subscription API.

The fixture supplies model output; Codex itself must execute the shell command
and persist/resume the thread. No live credential or provider call is used.
"""

import contextlib
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import shim

VERSION = "0.167.0"
MODEL = "gpt-6.1-terra"
PACKAGE_SHA256 = "3bbf75e83de6b58aed28f2cf6e33ad423ec31fd4700a1457b94be557ea5a7c6e"


def _artifact(variable):
    path = Path(os.environ[variable])
    if not path.is_absolute() and "TEST_SRCDIR" in os.environ:
        path = (
            Path(os.environ["TEST_SRCDIR"])
            / os.environ.get("TEST_WORKSPACE", "_main")
            / path
        )
    assert path.is_file(), f"missing required test artifact: {path}"
    return path


def _members(archive, prefix=""):
    return {
        name.removeprefix(prefix): member
        for member in archive.getmembers()
        if (name := member.name.removeprefix("./").rstrip("/"))
        and name.startswith(prefix)
    }


def test_runtime_layer_preserves_the_complete_official_package():
    package_path = _artifact("CODEX_PACKAGE_ARCHIVE")
    with package_path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == PACKAGE_SHA256
    with (
        tarfile.open(package_path) as package,
        tarfile.open(_artifact("CODEX_RUNTIME_LAYER")) as layer,
    ):
        expected = _members(package)
        actual = _members(layer, "usr/local/")
        assert actual.keys() == expected.keys()
        for name, original in expected.items():
            packaged = actual[name]
            assert packaged.type == original.type, name
            assert packaged.mode == original.mode, name
            assert packaged.linkname == original.linkname, name
            if original.isfile():
                assert packaged.size == original.size, name
                with (
                    package.extractfile(original) as source,
                    layer.extractfile(packaged) as destination,
                ):
                    assert (
                        hashlib.file_digest(source, "sha256").digest()
                        == hashlib.file_digest(destination, "sha256").digest()
                    ), name


@pytest.fixture(scope="module")
def cli_tmp_root():
    # Pytest's root-owned temporary ancestors are private to the runner. The
    # real shim drops its CLI to the guest uid, so use a dedicated traversable
    # tree without changing permissions on pytest's shared directories.
    with tempfile.TemporaryDirectory(prefix="codex-compat-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o755)
        yield root


@pytest.fixture
def cli_tmp_path(cli_tmp_root):
    with tempfile.TemporaryDirectory(prefix="test-", dir=cli_tmp_root) as directory:
        path = Path(directory)
        path.chmod(0o755)
        yield path


@pytest.fixture(scope="module")
def runtime_root(cli_tmp_root):
    root = cli_tmp_root / "runtime"
    root.mkdir()
    root.chmod(0o755)
    with tarfile.open(_artifact("CODEX_RUNTIME_LAYER")) as layer:
        layer.extractall(root, filter="data")
    return root / "usr/local"


def test_packaged_cli_and_manifest_identify_the_candidate(runtime_root):
    manifest = json.loads((runtime_root / "codex-package.json").read_text())
    assert manifest == {
        "layoutVersion": 1,
        "version": VERSION,
        "target": "x86_64-unknown-linux-musl",
        "variant": "codex",
        "entrypoint": "bin/codex",
        "execServer": "bin/codex-exec-server",
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }
    result = subprocess.run(
        [str(runtime_root / manifest["entrypoint"]), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        **shim._cli_privilege_kwargs(),
    )
    assert result.stdout.strip() == f"codex-cli {VERSION}"


# The catalog shape and SSE items are from the pinned producer's
# protocol/openai_models.rs and core/tests/common/responses.rs. Code-mode-only
# is Terra's tool mode; the fixture keeps the ordinary Responses transport.
CATALOG = {
    "models": [
        {
            "slug": MODEL,
            "display_name": "Terra compatibility fixture",
            "description": "Local producer compatibility test",
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [
                {"effort": "high", "description": "high"},
                {"effort": "xhigh", "description": "xhigh"},
            ],
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 0,
            "support_verbosity": True,
            "default_verbosity": "low",
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "context_window": 500000,
            "experimental_supported_tools": [],
            "tool_mode": "code_mode_only",
            "base_instructions": "Use the requested tool and report its output.",
        }
    ]
}


class SubscriptionFixture:
    def __init__(self):
        self.requests = []
        self.responses = 0
        self.proofs = []
        self.lock = threading.Lock()

    def handle(self, method, path, headers, body):
        with self.lock:
            self.requests.append((method, path, headers, body))
            if method == "GET" and path == "/backend-api/codex/models":
                return 200, "application/json", json.dumps(CATALOG).encode()
            if method == "POST" and path == "/backend-api/codex/responses":
                self.responses += 1
                response_id = f"response-{self.responses}"
                if self.responses % 2:
                    marker = f"codex-proof-{uuid.uuid4().hex}"
                    call_id = f"call-{self.responses}"
                    self.proofs.append((call_id, marker))
                    command = json.dumps(
                        {
                            "cmd": f"printf {marker}",
                            "shell": "/bin/sh",
                            "login": False,
                            "yield_time_ms": 1000,
                        }
                    )
                    item = {
                        "type": "custom_tool_call",
                        "call_id": call_id,
                        "name": "exec",
                        "input": f"text(JSON.stringify(await tools.exec_command({command})));",
                    }
                else:
                    # Deliberately completes even when the tool failed. The
                    # test must inspect real tool output, not this model text.
                    item = {
                        "type": "message",
                        "role": "assistant",
                        "id": f"message-{self.responses}",
                        "content": [
                            {"type": "output_text", "text": "fixture complete"}
                        ],
                    }
                events = [
                    {"type": "response.created", "response": {"id": response_id}},
                    {"type": "response.output_item.done", "item": item},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": response_id,
                            "usage": {
                                "input_tokens": 100,
                                "input_tokens_details": {"cached_tokens": 10},
                                "output_tokens": 20,
                                "output_tokens_details": {"reasoning_tokens": 5},
                                "total_tokens": 120,
                            },
                        },
                    },
                ]
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    for event in events
                )
                return 200, "text/event-stream", payload.encode()
            return 404, "application/json", b"{}"

    def turns(self):
        return [
            body
            for method, path, _, body in self.requests
            if method == "POST" and path == "/backend-api/codex/responses"
        ]

    def tool_output(self, call_id):
        for request in self.turns():
            for item in request.get("input", []):
                if (
                    item.get("type") == "custom_tool_call_output"
                    and item.get("call_id") == call_id
                ):
                    output = item["output"]
                    return (
                        [output]
                        if isinstance(output, str)
                        else [part.get("text", "") for part in output]
                    )
        raise AssertionError(f"no real tool output for {call_id}")

    def assert_shell_proof(self, call_id, marker):
        for text in self.tool_output(call_id):
            try:
                result = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(result, dict) and result.get("output") == marker:
                assert result.get("exit_code") == 0, result
                assert result.get("session_id") is None, result
                return
        raise AssertionError(f"shell proof missing for {call_id}")

    def assert_authorized(self):
        protected = [
            headers
            for _, path, headers, _ in self.requests
            if path in {"/backend-api/codex/models", "/backend-api/codex/responses"}
        ]
        assert protected, "no subscription request reached the fixture"
        for headers in protected:
            assert headers.get("authorization") == (
                "Bearer " + shim.CodexProcess._dummy_jwt()
            )


@pytest.fixture
def subscription(monkeypatch):
    fixture = SubscriptionFixture()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._respond()

        def do_POST(self):
            self._respond()

        def _respond(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            status, content_type, payload = fixture.handle(
                self.command,
                urlsplit(self.path).path,
                {key.lower(): value for key, value in self.headers.items()},
                body,
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        shim.CODEX_SUBSCRIPTION_BASE_URL_ENV,
        f"http://127.0.0.1:{server.server_port}/backend-api/",
    )
    monkeypatch.delenv(shim.AGENT_MCP_URL_ENV, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-producer")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-reach-producer")
    monkeypatch.setattr(shim, "TURN_READ_TIMEOUT", 20)
    monkeypatch.setattr(shim, "INIT_READ_TIMEOUT", 20)
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def _producer(root, workspace):
    shim._ensure_cli_dir(str(workspace))
    process = shim.CodexProcess(str(workspace), str(root / "bin/codex"))
    try:
        yield process
    finally:
        process._close_process(kill=True)


def test_real_model_discovery_sends_subscription_authorization(
    runtime_root, cli_tmp_path, subscription
):
    with _producer(runtime_root, cli_tmp_path / "workspace") as process:
        process._spawn()
        models = process._request("model/list", {})["data"]
        terra = next(model for model in models if model["id"] == MODEL)
        assert terra["displayName"] == "Terra compatibility fixture"
        assert any(
            level["reasoningEffort"] == "xhigh"
            for level in terra["supportedReasoningEfforts"]
        )
    assert any(
        path == "/backend-api/codex/models" for _, path, _, _ in subscription.requests
    )
    subscription.assert_authorized()


def test_real_terra_executes_and_resumes_its_persisted_thread(
    runtime_root, cli_tmp_path, subscription
):
    workspace = cli_tmp_path / "workspace"
    with _producer(runtime_root, workspace) as process:
        first = process.turn(
            "Run the requested shell proof.", model="terra", effort="xhigh"
        )
    with _producer(runtime_root, workspace) as resumed:
        second = resumed.turn(
            "Run a fresh shell proof.",
            session_id=first["session_id"],
            model="terra",
            effort="xhigh",
        )
    assert second["session_id"] == first["session_id"]
    assert first["session_id"]
    for record in (first, second):
        assert record["terminal_reason"] == "completed"
        assert record["provider_model"] == MODEL
        assert record["effort"] == "xhigh"
        assert record["usage"]["input_tokens"] > 0
        assert record["usage"]["output_tokens"] > 0
        assert any(activity["type"] == "bash" for activity in record["activities"])
    assert len(subscription.proofs) == 2
    for call_id, marker in subscription.proofs:
        subscription.assert_shell_proof(call_id, marker)
    for request in subscription.turns():
        assert request["model"] == MODEL
        assert request["reasoning"]["effort"] == "xhigh"
    subscription.assert_authorized()


def test_missing_code_mode_host_cannot_pass_with_completed_text(
    cli_tmp_path, subscription
):
    root = cli_tmp_path / "incomplete"
    root.mkdir()
    root.chmod(0o755)
    with tarfile.open(_artifact("CODEX_RUNTIME_LAYER")) as layer:
        layer.extractall(root, filter="data")
    package = root / "usr/local"
    (package / "bin/codex-code-mode-host").unlink()
    with _producer(package, cli_tmp_path / "workspace") as process:
        record = process.turn(
            "Run the requested shell proof.", model="terra", effort="xhigh"
        )
    assert record["terminal_reason"] == "completed"
    assert record["result"] == "fixture complete"
    assert len(subscription.proofs) == 1
    call_id, marker = subscription.proofs[0]
    with pytest.raises(AssertionError, match="shell proof missing"):
        subscription.assert_shell_proof(call_id, marker)
    assert "codex-code-mode-host" in "\n".join(subscription.tool_output(call_id))
