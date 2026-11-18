"""Run the site check from the release image with the PostSync restrictions.

The workflow runner provides Docker. Missing Docker or a failed browser launch
fails this test; it never falls back to host Node or a host Chromium executable.
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
    with tempfile.TemporaryDirectory(prefix="site-check-engine-", dir="/tmp") as tmp:
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


def job_container():
    rendered = run(
        [
            os.environ["HELM_BIN"],
            "template",
            "jomcgi-site",
            str(Path(__file__).resolve().parent / "chart"),
            "--set-string",
            "image.digest=sha256:" + "0" * 64,
        ],
        env=os.environ.copy(),
    )
    job = next(
        doc
        for doc in yaml.safe_load_all(rendered)
        if doc and doc["kind"] == "Job" and doc["metadata"]["name"] == "site-check"
    )
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    assert container["resources"]["limits"]["memory"] == "1536Mi"
    assert container["command"] == ["/app/projects/jomcgi-site/check/site-check"]
    assert container["env"] == [
        {"name": "HOME", "value": "/tmp"},
        {"name": "TMPDIR", "value": "/tmp"},
    ]
    return container


def test_packaged_browser_under_postsync_restrictions():
    container = job_container()
    with docker_engine() as (docker, env):
        image_id, config = image_config(Path(os.environ["SITE_CHECK_IMAGE"]))
        assert config["User"] in ("65532", "65532:65532")
        run([os.path.abspath(os.environ["SITE_CHECK_LOADER"])], env=env, timeout=180)
        observed = json.loads(run([docker, "image", "inspect", image_id], env=env))[0]
        assert observed["Id"] == image_id
        assert observed["Config"]["User"] == config["User"]
        name = "site-check-smoke-" + uuid.uuid4().hex
        command = [
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
            "--memory=1536m",
            # Docker defaults to RuntimeDefault seccomp. /tmp must allow exec:
            # the Job unpacks the browser into this sole writable volume.
            "--tmpfs=/tmp:rw,exec,nosuid,size=1g,uid=65532,gid=65532",
            "--env=HOME=/tmp",
            "--env=TMPDIR=/tmp",
            "--entrypoint",
            container["command"][0],
            image_id,
            "--self-test",
        ]
        print(json.dumps({"imageId": image_id, "command": command}), flush=True)
        try:
            output = run(command, env=env, timeout=560)
            print(output, flush=True)
            assert "image smoke: dev release passed both tiers" in output
            assert "image smoke: prod release passed both tiers" in output
            assert "image smoke: rendered CSS contrast violation rejected" in output
        finally:
            subprocess.run(
                [docker, "rm", "--force", name],
                env=env,
                capture_output=True,
                timeout=15,
                check=False,
            )


def test_image_has_the_sqlite_library_that_chromium_nss_loads():
    # Chromium initialises NSS on its first HTTPS page, and NSS loads
    # libsqlite3.so.0. Without it the browser aborts (FATAL nss_util.cc,
    # nss_error=-5925) at the first real navigation. The self-test above
    # browses a local HTTP fixture, so it would not catch the missing library.
    with docker_engine() as (docker, env):
        image_id, _ = image_config(Path(os.environ["SITE_CHECK_IMAGE"]))
        run([os.path.abspath(os.environ["SITE_CHECK_LOADER"])], env=env, timeout=180)
        output = run(
            [
                docker,
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--entrypoint",
                "/bin/sh",
                image_id,
                "-c",
                "ls /usr/lib/libsqlite3.so.0",
            ],
            env=env,
            timeout=60,
        )
        assert "libsqlite3.so.0" in output
