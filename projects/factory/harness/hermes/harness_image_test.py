"""The slim Hermes harness image holds the fork's runner and a working Hermes.

The test reads the built image's OCI layout. It checks the layers, the runner
binary and the Hermes version stamp. It unpacks the image and starts its
Hermes gateway the way kagent-hermes does, against a fake OpenAI-compatible
model on loopback, and sends the runner's /v1/responses calls. Where the host
allows an unprivileged user namespace, the gateway and `kagent-hermes --check`
run chrooted in the image as UID 65532. The test needs no network and no
model credential.
"""

from __future__ import annotations

import glob
import gzip
import hashlib
import json
import os
import secrets
import shutil
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

RUNNER = "usr/local/bin/kagent-hermes"
GATEWAY = "/opt/hermes/.venv/bin/hermes"
HERMES_VERSION = "0.23.4"
# The v0.23.4 tag's commit in NousResearch/hermes-agent.
HERMES_COMMIT = "8fc0374447313e276f6b9a2764bfc750190c966a"
MODEL = "fake/model"
# Upstream parts the slim image leaves out (slim.py DROP, and the apt layer).
_LEFT_OUT = (
    "opt/hermes/tools/chromium-",
    "opt/hermes/tools/ffmpeg-",
    "opt/hermes/tools/node-",
    "opt/hermes/apps/",
    "opt/hermes/node_modules/",
    "usr/bin/gcc",
    "usr/bin/docker",
    "usr/lib/x86_64-linux-gnu/libLLVM",
)
# kagent-hermes' disabled toolset list (driver.DisabledToolsets at the fork pin).
_DISABLED_TOOLSETS = [
    "a2a", "browser", "catalog", "clarify", "code_execution", "computer_use",
    "connections", "cronjob", "delegation", "desktop_ui", "discord",
    "discord_admin", "feishu_doc", "feishu_drive", "file", "image_gen",
    "kanban", "memory", "project", "search", "session_search", "skills",
    "terminal", "todo", "tts", "video", "video_gen", "vision", "web", "x_search",
]  # fmt: skip


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


def _layers(layout: Path) -> list[Path]:
    return [_blob(layout, layer["digest"]) for layer in _manifest(layout)["layers"]]


def _image() -> Path:
    return Path(os.environ["IMAGE_LAYOUT"])


def _member_digest(layout: Path, name: str) -> str:
    for blob in _layers(layout):
        with tarfile.open(blob, "r|*") as layer:
            for member in layer:
                if member.name.removeprefix("./") == name and member.isfile():
                    data = layer.extractfile(member).read()  # type: ignore[union-attr]
                    return hashlib.sha256(data).hexdigest()
    raise AssertionError(f"{name} not in {layout}")


def _userns() -> tuple[bool, str]:
    if not shutil.which("unshare"):
        return False, "no unshare binary on the test host"
    probe = subprocess.run(
        ["unshare", "-U", "--map-user=65532", "--map-group=65532", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0, probe.stderr.strip()


USERNS, USERNS_ERROR = _userns()
# In CI the chrooted cases must run: a missing user namespace fails there
# instead of skipping. BUILD passes CI in through env_inherit.
IN_CI = bool(os.environ.get("CI"))


def _need_userns() -> None:
    if USERNS:
        return
    if IN_CI:
        pytest.fail(
            "the CI test host has no unprivileged user namespace, so the "
            f"chrooted image cases cannot run: {USERNS_ERROR}"
        )
    pytest.skip(f"needs an unprivileged user namespace: {USERNS_ERROR}")


def test_ci_runs_the_chrooted_cases() -> None:
    if IN_CI:
        _need_userns()


@pytest.fixture(scope="module")
def rootfs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for blob in _layers(_image()):
            with tarfile.open(blob, "r|*") as layer:
                for member in layer:
                    if member.isdev() or "/.wh." in "/" + member.name:
                        continue
                    member.uid = member.gid = os.getuid()
                    layer.extract(member, root, filter="fully_trusted")
        # UID 65532 cannot write the image's own tree. Outside the image the
        # test user owns every file, so make the tree read-only to match;
        # /data (the runner's durable directory) and /tmp stay writable.
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                rel = path.relative_to(root).as_posix()
                if rel not in ("data", "tmp") and not rel.startswith("data/"):
                    path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        yield root
        for path in root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(stat.S_IRWXU)


def _in_image(root: Path, argv: list[str], env: dict[str, str]) -> list[str]:
    """Return argv to run a command chrooted in the image as UID 65532."""
    pairs = [f"{k}={v}" for k, v in env.items()]
    return [
        "unshare",
        "-U",
        "--map-user=65532",
        "--map-group=65532",
        "-R",
        str(root),
        "/usr/bin/env",
        "-i",
        *pairs,
        *argv,
    ]


def _image_env() -> dict[str, str]:
    env = {}
    for entry in _config(_image())["config"]["Env"]:
        name, _, value = entry.partition("=")
        env[name] = value
    return env


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


def _hermes_config(base_url: str) -> dict:
    """config.yaml as kagent-hermes renders it (driver.RenderConfig)."""
    return {
        "model": {
            "default": MODEL,
            "provider": "custom",
            "base_url": base_url,
            "api_key": "kagent-credential-injected",
        },
        "toolsets": [],
        "platform_toolsets": {"api_server": []},
        "agent": {"disabled_toolsets": _DISABLED_TOOLSETS},
        "memory": {
            "memory_enabled": False,
            "user_profile_enabled": False,
            "provider": "",
        },
        "approvals": {
            "mode": "manual",
            "timeout": 10,
            "cron_mode": "deny",
            "single_query_mode": "deny",
            "unattended_mode": "deny",
        },
        "security": {"allow_lazy_installs": False},
        "auxiliary": {"title_generation": {"enabled": False}},
    }


@pytest.fixture(scope="module")
def gateway(rootfs):
    _FakeModel.calls.clear()
    model = ThreadingHTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    port, key = _free_port(), secrets.token_hex(32)
    base_url = f"http://127.0.0.1:{model.server_address[1]}/v1"
    # /tmp, so the --check cases can own /data.
    data = Path(tempfile.mkdtemp(dir=rootfs / "tmp"))
    home = data / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(_hermes_config(base_url)))
    api = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": str(port),
        "API_SERVER_KEY": key,
    }
    if USERNS:
        inner = "/" + data.relative_to(rootfs).as_posix()
        env = {**_image_env(), "HOME": inner, "HERMES_HOME": f"{inner}/hermes", **api}
        argv = _in_image(rootfs, [GATEWAY, "gateway"], env)
        run_env = None
    else:
        # No user namespace: run the image's interpreter on the host, with
        # the same bootstrap as the image's launcher script.
        (python,) = glob.glob(str(rootfs / "opt/hermes/tools/python-*/bin/python3"))
        site = rootfs / "opt/hermes/.venv/lib/python3.14/site-packages"
        bootstrap = (
            "import os, site, sys; sys.argv[0] = 'hermes'; site.addsitedir(os.environ['HERMES_SITE']); "
            "from hermes_cli.main import main; sys.exit(main())"
        )
        argv = [python, "-P", "-c", bootstrap, "gateway"]
        run_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(data),
            "TMPDIR": str(data),
            "HERMES_HOME": str(home),
            "PYTHONPATH": f"{rootfs / 'opt/hermes'}:{site}",
            "HERMES_SITE": str(site),
            **api,
        }
    log = open(data / "gateway.log", "w+b")
    proc = subprocess.Popen(
        argv, env=run_env, cwd=home, stdout=log, stderr=subprocess.STDOUT
    )
    try:
        deadline = time.monotonic() + 240
        while _get(f"http://127.0.0.1:{port}/health") != 200:
            if proc.poll() is not None or time.monotonic() > deadline:
                log.seek(0)
                pytest.fail(
                    "gateway never became healthy:\n"
                    + log.read().decode(errors="replace")[-4000:]
                )
            time.sleep(1)
        yield {"url": f"http://127.0.0.1:{port}", "key": key, "home": home}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        model.shutdown()
        log.close()


def _respond(gw: dict, session: str, text: str) -> dict:
    """Send one turn the way kagent-hermes does (driver client.respond)."""
    body = {
        "input": text,
        "instructions": "You answer for the homelab server.",
        "store": True,
        "conversation": session,
    }
    req = urllib.request.Request(
        gw["url"] + "/v1/responses",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + gw["key"],
            "X-Hermes-Session-Key": session,
            "Idempotency-Key": "kagent-task-" + secrets.token_hex(8),
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _answer(out: dict) -> str:
    assert out["status"] in ("completed", "incomplete"), out
    return "".join(
        part["text"]
        for item in out["output"]
        if item.get("type") == "message"
        and item.get("role") == "assistant"
        and item.get("phase") != "commentary"
        for part in item["content"]
        if part.get("type") == "output_text"
    )


def test_image_is_slim_and_runs_the_runner() -> None:
    layout = _image()
    layers = _manifest(layout)["layers"]
    assert len(layers) == 4, "Debian, Hermes, runner, /data"
    config = _config(layout)["config"]
    assert config["Entrypoint"] == ["/" + RUNNER]
    assert not config.get("Cmd")
    assert config["User"] == "65532:65532"
    assert config["WorkingDir"] == "/data"
    assert "HERMES_HOME=/data/hermes" in config["Env"]
    assert "HOME=/data" in config["Env"]
    # The upstream image is 1149 MB as pushed; the slim one about 333 MB.
    assert sum(layer["size"] for layer in layers) < 400 * 10**6
    for blob in _layers(layout):
        with tarfile.open(blob, "r|*") as layer:
            for member in layer:
                assert not member.name.removeprefix("./").startswith(_LEFT_OUT)


def _content_digest(blob: Path) -> str:
    """Return the sha256 of a layer's uncompressed tar."""
    with blob.open("rb") as raw:
        gzipped = raw.read(2) == b"\x1f\x8b"
    digest = hashlib.sha256()
    with gzip.open(blob) if gzipped else blob.open("rb") as src:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_base_and_runner_layers_are_the_upstream_layers() -> None:
    image = _layers(_image())
    (runner,) = _layers(Path(os.environ["RUNNER_LAYOUT"]))
    debian = _layers(Path(os.environ["BASE_LAYOUT"]))[0]
    assert _content_digest(image[0]) == _content_digest(debian)
    assert _content_digest(image[2]) == _content_digest(runner)


def test_runner_is_the_fork_hermes_runner() -> None:
    fork = _member_digest(Path(os.environ["RUNNER_LAYOUT"]), RUNNER)
    assert _member_digest(_image(), RUNNER) == fork


def test_image_is_hermes_v0_23_4(rootfs) -> None:
    stamp = json.loads((rootfs / "opt/hermes/install-stamp.json").read_text())
    assert stamp["baseVersion"] == HERMES_VERSION
    assert stamp["commit"] == HERMES_COMMIT
    assert stamp["dirty"] is False
    assert (rootfs / GATEWAY.lstrip("/")).is_symlink()


def _varint(n: int) -> bytes:
    out = b""
    while True:
        byte, n = n & 0x7F, n >> 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def _write_bundle(data: Path, config: dict) -> str:
    """Write kagent's bootstrap bundle; return its descriptor JSON."""
    revision = hashlib.sha256(b"homelab-hermes-test").hexdigest()
    config_json = json.dumps(config).encode()
    card_json = json.dumps({"name": "homelab", "url": "http://127.0.0.1/"}).encode()
    digest = hashlib.sha256((1).to_bytes(4, "big"))
    for field in (revision.encode(), config_json, card_json):
        digest.update(len(field).to_bytes(8, "big") + field)
    sha = digest.hexdigest()
    bundle = b"\x08\x01"
    for tag, value in (
        (0x12, revision.encode()),
        (0x1A, config_json),
        (0x22, card_json),
        (0x2A, sha.encode()),
    ):
        bundle += bytes([tag]) + _varint(len(value)) + value
    directory = data / "kagent" / "bootstrap"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bundle.pb").write_bytes(bundle)
    return json.dumps(
        {
            "format_version": 1,
            "control_plane_url": "http://127.0.0.1:1",
            "revision": revision,
            "sha256": sha,
        }
    )


def _runner_config(gateway_command: list[str]) -> dict:
    """The compiler's configuration for a homelab Agent (config.Production)."""
    return {
        "control_plane_url": "http://127.0.0.1:1",
        "version": 1,
        "gateway_command": gateway_command,
        "model": "deepseek/deepseek-v4.1-flash-1017",
        "base_url": "https://openrouter.ai/api/v1",
        "api_server_port": 8642,
        "startup_timeout_millis": 120000,
        "turn_timeout_millis": 600000,
        "stop_grace_millis": 20000,
        "max_response_bytes": 8 << 20,
    }


@pytest.mark.parametrize(
    ("command", "ok"),
    [([GATEWAY, "gateway"], True), (["/opt/hermes/.venv/bin/absent"], False)],
)
def test_runner_check_finds_hermes_in_the_image(rootfs, command, ok) -> None:
    _need_userns()
    # kagent-hermes reads its bundle from /data/kagent/bootstrap, so each
    # case starts from an empty /data.
    data = rootfs / "data"
    for child in data.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    descriptor = _write_bundle(data, _runner_config(command))
    env = {**_image_env(), "KAGENT_BOOTSTRAP_JSON": descriptor}
    out = subprocess.run(
        _in_image(rootfs, ["/" + RUNNER, "--check"], env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (out.returncode == 0) == ok, out.stderr
    if not ok:
        assert "find Hermes gateway executable" in out.stderr


def test_gateway_answers_the_runner_turns(gateway) -> None:
    session = "kagent-" + secrets.token_hex(16)
    first = _respond(gateway, session, "Who runs the factory?")
    assert _answer(first) == "Hermes answer 1"

    # The next turn on the same session key and conversation carries the first.
    second = _respond(gateway, session, "And who reviews it?")
    assert _answer(second) == "Hermes answer 2"
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
    assert turns[1]["auth"] == "Bearer kagent-credential-injected"

    # A different session is a different conversation.
    other = _respond(gateway, "kagent-" + secrets.token_hex(16), "Hello?")
    assert _answer(other) == "Hermes answer 1"


def test_model_calls_carry_no_tools(gateway) -> None:
    if not _FakeModel.calls:
        _respond(gateway, "kagent-" + secrets.token_hex(16), "What tools do you have?")
    for call in _FakeModel.calls:
        body = call["body"]
        assert not body.get("tools"), (
            f"tools reached the model: {[t['function']['name'] for t in body['tools']]}"
        )
        system = body["messages"][0]["content"]
        assert "Platform: api_server" in system
