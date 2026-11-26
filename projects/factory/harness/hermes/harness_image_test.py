"""The Hermes harness image answers an A2A turn through the real Hermes gateway.

The test unpacks the built image, starts its kagent-hermes adapter and the
pinned Hermes gateway against a fake OpenAI-compatible model on loopback, and
sends A2A SendMessage turns. It needs no network and no model credential.
"""

from __future__ import annotations

import glob
import json
import os
import socket
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ADAPTER = "usr/local/bin/kagent-hermes"
HERMES_VERSION = "0.23.4"
# The v0.23.4 tag's commit in NousResearch/hermes-agent.
HERMES_COMMIT = "8fc0374447313e276f6b9a2764bfc750190c966a"
MODEL = "fake/model"
# Large image parts the gateway never loads with every toolset off.
_SKIP = (
    "opt/hermes/tools/chromium-",
    "opt/hermes/tools/ffmpeg-",
    "opt/hermes/tools/node-",
    "opt/hermes/apps/",
    "opt/hermes/node_modules/",
    "opt/hermes/ui-tui/",
    "opt/hermes/uv-cache/",
)


def _blob(layout: Path, digest: str) -> Path:
    algo, hex_digest = digest.split(":", 1)
    return layout / "blobs" / algo / hex_digest


def _manifest(layout: Path) -> dict:
    index = json.loads((layout / "index.json").read_text())
    (entry,) = index["manifests"]
    manifest = json.loads(_blob(layout, entry["digest"]).read_text())
    if "manifests" in manifest:  # An image index: take its one platform.
        (entry,) = manifest["manifests"]
        manifest = json.loads(_blob(layout, entry["digest"]).read_text())
    return manifest


def _config(layout: Path) -> dict:
    return json.loads(_blob(layout, _manifest(layout)["config"]["digest"]).read_text())


def _layers(layout: Path) -> list[str]:
    return [layer["digest"] for layer in _manifest(layout)["layers"]]


def _wanted(member: tarfile.TarInfo) -> bool:
    name = member.name.removeprefix("./")
    if "/.wh." in "/" + name or not (
        name.startswith("opt/hermes/")
        or name.startswith("etc/hermes/")
        or name == ADAPTER
    ):
        return False
    if name.startswith(_SKIP):
        return False
    # Absolute links point into the image root, which the test does not own.
    if member.issym() and member.linkname.startswith("/"):
        return False
    return member.isfile() or member.isdir() or member.issym()


def _unpack(layout: Path, dest: Path) -> None:
    for digest in _layers(layout):
        with tarfile.open(_blob(layout, digest), "r|*") as layer:
            for member in layer:
                if _wanted(member):
                    member.uid = member.gid = os.getuid()
                    layer.extract(member, dest, filter="tar")


@pytest.fixture(scope="module")
def rootfs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _unpack(Path(os.environ["IMAGE_LAYOUT"]), root)
        # The image runs as UID 65532 and cannot write its own tree: make the
        # unpacked copy read-only so the gateway gets the same constraint.
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        yield root
        for path in root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(stat.S_IRWXU)


class _FakeModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible chat completions server that answers by turn."""

    calls: list[dict] = []

    def log_message(self, *args):  # noqa: D102
        pass

    def _send(self, body: str, ctype: str = "application/json") -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._send(
            json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": MODEL, "object": "model", "context_length": 128000}
                    ],
                }
            )
        )

    def do_POST(self):  # noqa: N802
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
        )
        _FakeModel.calls.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        users = [m for m in body.get("messages", []) if m.get("role") == "user"]
        text = f"Hermes answer {len(users)}"
        usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        if not body.get("stream"):
            self._send(
                json.dumps(
                    {
                        "id": "c",
                        "object": "chat.completion",
                        "model": MODEL,
                        "usage": usage,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": text},
                            }
                        ],
                    }
                )
            )
            return
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            },
        ]
        stream = "".join(
            "data: "
            + json.dumps(
                {"id": "c", "object": "chat.completion.chunk", "model": MODEL, **c}
            )
            + "\n\n"
            for c in chunks
        )
        self._send(stream + "data: [DONE]\n\n", "text/event-stream")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError:
        return 0


def _send_message(url: str, message_id: str, context_id: str, text: str) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": message_id,
                "contextId": context_id,
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            }
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="module")
def harness(rootfs):
    _FakeModel.calls.clear()
    model = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    a2a_port, ready_port, hermes_port = _free_port(), _free_port(), _free_port()
    (python,) = glob.glob(str(rootfs / "opt/hermes/tools/python-*/bin/python3"))
    site = rootfs / "opt/hermes/.venv/lib/python3.14/site-packages"
    with tempfile.TemporaryDirectory() as data:
        home = Path(data) / "hermes"
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": data,
            "TMPDIR": data,
            "XDG_RUNTIME_DIR": data,
            "PYTHONPATH": f"{rootfs / 'opt/hermes'}:{site}",
            "HERMES_SITE": str(site),
            "HERMES_MODEL": MODEL,
            "HERMES_BASE_URL": f"http://127.0.0.1:{model.server_address[1]}/v1",
            # Dropped by the adapter: a messaging token must not start a platform.
            "DISCORD_BOT_TOKEN": "not-a-token",
        }
        # The image's launcher resolves an absolute interpreter link, so the
        # test runs the same bootstrap with the unpacked interpreter.
        bootstrap = (
            "import os, site, sys; sys.argv[0] = 'hermes'; site.addsitedir(os.environ['HERMES_SITE']); "
            "from hermes_cli.main import main; sys.exit(main())"
        )
        log = open(Path(data) / "adapter.log", "w+b")
        proc = subprocess.Popen(
            [
                str(rootfs / ADAPTER),
                f"-listen=127.0.0.1:{a2a_port}",
                f"-ready=127.0.0.1:{ready_port}",
                f"-home={home}",
                f"-hermes-port={hermes_port}",
                "--",
                python,
                "-P",
                "-c",
                bootstrap,
                "gateway",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 240
            while _get(f"http://127.0.0.1:{ready_port}/readyz") != 200:
                if proc.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    pytest.fail(
                        "harness never became ready:\n"
                        + log.read().decode(errors="replace")[-4000:]
                    )
                time.sleep(1)
            yield {"url": f"http://127.0.0.1:{a2a_port}/", "home": home, "log": log}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            model.shutdown()
            log.close()


def test_image_keeps_the_pinned_hermes_and_adds_the_adapter() -> None:
    base = _layers(Path(os.environ["BASE_LAYOUT"]))
    image = _layers(Path(os.environ["IMAGE_LAYOUT"]))
    assert image[: len(base)] == base
    assert len(image) == len(base) + 1
    config = _config(Path(os.environ["IMAGE_LAYOUT"]))["config"]
    assert config["Entrypoint"] == ["/usr/local/bin/kagent-hermes"]
    assert not config.get("Cmd")
    assert config["User"] == "65532:65532"
    assert "HERMES_HOME=/data/hermes" in config["Env"]


def test_image_is_hermes_v0_23_4(rootfs) -> None:
    stamp = json.loads((rootfs / "opt/hermes/install-stamp.json").read_text())
    assert stamp["baseVersion"] == HERMES_VERSION
    assert stamp["commit"] == HERMES_COMMIT
    assert stamp["dirty"] is False


def test_a2a_turn_gets_a_hermes_answer(harness) -> None:
    first = _send_message(harness["url"], "m-1", "ctx-homelab", "Who runs the factory?")
    task = first["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", first
    assert task["contextId"] == "ctx-homelab"
    assert task["status"]["message"]["parts"][0]["text"] == "Hermes answer 1"

    # The second turn in the same context carries the first one.
    second = _send_message(harness["url"], "m-2", "ctx-homelab", "And who reviews it?")
    assert (
        second["result"]["task"]["status"]["message"]["parts"][0]["text"]
        == "Hermes answer 2"
    ), second
    turns = [c for c in _FakeModel.calls if c["path"].endswith("/chat/completions")]
    assert len(turns) == 2, "one model call per turn and no title call"
    history = [
        m["content"]
        for m in turns[1]["body"]["messages"]
        if m["role"] in ("user", "assistant")
    ]
    assert history == [
        "Who runs the factory?",
        "Hermes answer 1",
        "And who reviews it?",
    ]

    # A different context is a different conversation.
    other = _send_message(harness["url"], "m-3", "ctx-other", "Hello?")
    assert (
        other["result"]["task"]["status"]["message"]["parts"][0]["text"]
        == "Hermes answer 1"
    )


def test_model_calls_carry_no_tools(harness) -> None:
    if not _FakeModel.calls:
        _send_message(harness["url"], "m-tools", "ctx-tools", "What tools do you have?")
    for call in _FakeModel.calls:
        body = call["body"]
        assert not body.get("tools"), (
            f"tools reached the model: {[t['function']['name'] for t in body['tools']]}"
        )
        system = body["messages"][0]["content"]
        assert "Platform: api_server" in system


def test_gateway_runs_with_the_rendered_config(harness) -> None:
    cfg = yaml.safe_load((harness["home"] / "config.yaml").read_text())
    assert cfg["platform_toolsets"] == {"api_server": []}
    assert cfg["approvals"]["unattended_mode"] == "deny"
    hindsight = json.loads((harness["home"] / "hindsight" / "config.json").read_text())
    assert hindsight["memory_mode"] == "context"
    harness["log"].flush()
    harness["log"].seek(0)
    log = harness["log"].read().decode(errors="replace")
    assert "hermes gateway healthy" in log
