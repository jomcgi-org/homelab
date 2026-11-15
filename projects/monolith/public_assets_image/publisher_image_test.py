"""Execute the release publisher using the exact built public backend image.

This local-only Bazel test needs Docker, not a registry or S3 credentials. If
CI has no running engine, start a bounded private one; never skip the smoke or
substitute the pytest interpreter for the interpreter packaged in the image.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
import yaml


def image_config(layout: Path) -> tuple[str, dict]:
    def blob(descriptor):
        algorithm, digest = descriptor["digest"].split(":", 1)
        assert algorithm == "sha256" and len(digest) == 64
        body = (layout / "blobs" / algorithm / digest).read_bytes()
        assert hashlib.sha256(body).hexdigest() == digest
        return json.loads(body)

    index = json.loads((layout / "index.json").read_text())
    assert len(index["manifests"]) == 1
    manifest = blob(index["manifests"][0])
    config = blob(manifest["config"])
    assert config["os"] == "linux" and config["architecture"] == "amd64"
    return manifest["config"]["digest"], config["config"]


def run(command, *, env, timeout=30):
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, (
        f"command failed: {command}\n{result.stdout}\n{result.stderr}"
    )
    return result.stdout


@contextlib.contextmanager
def docker_engine():
    docker = shutil.which("docker")
    assert docker, "The packaged-image smoke requires Docker on the CI runner"
    env = os.environ.copy()
    # Engine selection must not accidentally use a developer's remote context.
    for key in (
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        env.pop(key, None)
    env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    available = subprocess.run(
        [docker, "info"], env=env, capture_output=True, timeout=15, check=False
    )
    if available.returncode == 0:
        print("Image smoke uses existing local Unix Docker engine")
        yield docker, env
        return

    dockerd = shutil.which("dockerd")
    assert dockerd, "No Docker engine is available and dockerd is not installed"
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    # Short path avoids Unix socket path limits in deeply nested Bazel runfiles.
    with tempfile.TemporaryDirectory(prefix="public-asset-engine-", dir="/tmp") as tmp:
        env["DOCKER_HOST"] = f"unix://{tmp}/docker.sock"
        # Do not inherit a host daemon's remote listeners or storage settings.
        (Path(tmp) / "daemon.json").write_text("{}")
        with open(Path(tmp) / "daemon.log", "w+") as log:
            daemon = subprocess.Popen(
                [
                    *prefix,
                    dockerd,
                    f"--config-file={tmp}/daemon.json",
                    f"--host={env['DOCKER_HOST']}",
                    f"--data-root={tmp}/data",
                    f"--exec-root={tmp}/exec",
                    f"--pidfile={tmp}/docker.pid",
                    "--storage-driver=vfs",
                    "--bridge=none",
                    "--iptables=false",
                    "--ip6tables=false",
                    "--ip-forward=false",
                    "--ip-masq=false",
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and daemon.poll() is None:
                    result = subprocess.run(
                        [docker, "info"],
                        env=env,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    if result.returncode == 0:
                        print("Image smoke started a private local Unix Docker engine")
                        yield docker, env
                        return
                    time.sleep(0.5)
                log.seek(0)
                pytest.fail("Private Docker engine failed to start:\n" + log.read())
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
                if prefix:
                    # The private daemon writes root-owned image layers. Restore
                    # ownership only inside this test's fresh temporary tree.
                    run(
                        [*prefix, "chown", "-R", f"{os.getuid()}:{os.getgid()}", tmp],
                        env=env,
                        timeout=30,
                    )


def container_command(docker, image_id, command, name):
    assert image_id.startswith("sha256:") and len(image_id) == 71
    assert len(command) == 3 and command[1:] == [
        "-m",
        "artifact.publish_public_assets",
    ]
    assert command[0].startswith("/")
    return [
        docker,
        "run",
        "--rm",
        f"--name={name}",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
        command[0],
        image_id,
        *command[1:],
        "--help",
    ]


def publisher_command():
    chart = Path(__file__).resolve().parents[2] / "monolith-public" / "chart"
    output = run(
        [
            os.environ["HELM_BIN"],
            "template",
            "monolith-public",
            str(chart),
            "--set",
            "frontend.image.repository=test.invalid/frontend",
            "--set",
            "frontend.image.digest=sha256:" + "1" * 64,
            "--set",
            "web.image.repository=test.invalid/backend",
            "--set",
            "web.image.digest=sha256:" + "2" * 64,
        ],
        env=os.environ.copy(),
    )
    job = next(
        doc
        for doc in yaml.safe_load_all(output)
        if doc
        and doc["kind"] == "Job"
        and doc["metadata"]["name"] == "monolith-public-publish-assets"
    )
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[-1] == "/work/immutable"
    return command[:-1]


def test_packaged_publisher_starts_without_credentials_or_network():
    image_id, config = image_config(Path(os.environ["PUBLIC_BACKEND_IMAGE"]))
    image_env = dict(item.split("=", 1) for item in config.get("Env", []))
    assert (
        not {
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        }
        & image_env.keys()
    )
    command = publisher_command()
    with docker_engine() as (docker, env):
        run(
            [os.path.abspath(os.environ["PUBLIC_BACKEND_LOADER"])], env=env, timeout=180
        )
        observed = json.loads(run([docker, "image", "inspect", image_id], env=env))[0]
        assert observed["Id"] == image_id
        assert observed["Config"]["Env"] == config["Env"]
        assert observed["Config"]["WorkingDir"] == config["WorkingDir"]
        name = "public-asset-publisher-" + uuid.uuid4().hex
        smoke = container_command(docker, image_id, command, name)
        try:
            output = run(smoke, env=env)
            assert "usage:" in output and "directory" in output
            assert "Append-only public asset publisher" in output
            print(
                json.dumps(
                    {
                        "imageId": image_id,
                        "command": command + ["--help"],
                        "stdout": output,
                    }
                )
            )
            # The storage SDK is imported lazily; prove it is packaged too,
            # using the same image interpreter without constructing a client.
            sdk = smoke[: smoke.index(image_id) + 1] + [
                "-c",
                (
                    "import boto3, botocore, ssl, sys; "
                    "assert ssl.get_default_verify_paths().cafile; "
                    "print(sys.executable, boto3.__file__, botocore.__file__)"
                ),
            ]
            print(run(sdk, env=env))
        finally:
            subprocess.run(
                [docker, "rm", "--force", name],
                env=env,
                capture_output=True,
                timeout=15,
                check=False,
            )


def test_smoke_uses_exact_interpreter_with_no_network_mounts_or_credentials():
    command = ["/packaged/venv/bin/python3", "-m", "artifact.publish_public_assets"]
    args = container_command("docker", "sha256:" + "a" * 64, command, "test")
    assert args[-5:] == [command[0], "sha256:" + "a" * 64, "-m", command[2], "--help"]
    assert {
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=ALL",
    } <= set(args)
    assert not {"-e", "--env", "-v", "--volume", "--mount", "--privileged"} & set(args)


@pytest.mark.parametrize(
    "command",
    [
        ["python3", "-m", "artifact.publish_public_assets"],
        ["/python", "-m", "app.main_public"],
    ],
)
def test_smoke_rejects_a_host_interpreter_or_wrong_module(command):
    with pytest.raises(AssertionError):
        container_command("docker", "sha256:" + "a" * 64, command, "test")


def test_existing_engine_is_local_and_is_never_stopped_or_pruned(monkeypatch):
    calls = []
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")

    def info(command, **kwargs):
        calls.append(command)
        assert kwargs["env"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
        assert "DOCKER_CONTEXT" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", info)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not start a daemon")
    )
    with docker_engine() as (docker, env):
        assert docker == "/usr/bin/docker"
        assert env["DOCKER_HOST"].startswith("unix://")
    assert calls == [["/usr/bin/docker", "info"]]


def test_missing_engine_fails_instead_of_skipping(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(AssertionError, match="requires Docker"), docker_engine():
        pytest.fail("cannot execute an image without its runtime")


def test_failed_packaged_command_is_a_test_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, "", "ModuleNotFoundError: artifact.publish_public_assets"
        ),
    )
    with pytest.raises(AssertionError, match="ModuleNotFoundError"):
        run(["docker", "run"], env={})
