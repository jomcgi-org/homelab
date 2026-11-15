"""The public release archives its exact immutable output before serving HTML."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module", params=[False, True], ids=["chart", "gke"])
def rendered(request):
    chart = Path(__file__).resolve().parents[1] / "monolith-public" / "chart"
    overlays = []
    if request.param:
        for env_name, filename in (
            ("DEPLOY_VALUES", "values.yaml"),
            ("GKE_VALUES", "values-gke.yaml"),
        ):
            overlays.extend(
                [
                    "--values",
                    os.environ.get(env_name, str(chart.parent / "deploy" / filename)),
                ]
            )
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith-public",
            str(chart),
            "--namespace",
            "monolith-public",
            *overlays,
            "--set",
            "frontend.image.repository=test.invalid/public-frontend",
            "--set",
            "frontend.image.digest=sha256:" + "1" * 64,
            "--set",
            "web.image.repository=test.invalid/public-backend",
            "--set",
            "web.image.digest=sha256:" + "2" * 64,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def named(documents, kind, name):
    return next(
        doc
        for doc in documents
        if doc["kind"] == kind and doc["metadata"]["name"] == name
    )


def publisher(documents):
    return named(documents, "Job", "monolith-public-publish-assets")


def test_publication_precedes_backend_then_frontend(rendered):
    job = publisher(rendered)
    assert job["metadata"]["namespace"] == "monolith-workflows"
    assert job["metadata"]["annotations"] == {
        "argocd.argoproj.io/hook": "PreSync",
        "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
    }
    assert job["spec"]["activeDeadlineSeconds"] == 600
    assert job["spec"]["backoffLimit"] == 1
    web = named(rendered, "Deployment", "monolith-public-web")
    frontend = named(rendered, "Deployment", "monolith-public-frontend")
    assert web["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "0"
    assert frontend["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "1"
    pod = job["spec"]["template"]["spec"]
    assert (
        pod["initContainers"][0]["image"]
        == frontend["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    assert (
        pod["containers"][0]["image"]
        == web["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    assert pod["initContainers"][0]["image"].endswith("@sha256:" + "1" * 64)
    assert pod["containers"][0]["image"].endswith("@sha256:" + "2" * 64)
    assert pod["initContainers"][0]["args"][1:] == [
        "/app/build/client/_app/immutable",
        "/work/immutable",
    ]
    assert pod["containers"][0]["command"] == [
        "/projects/monolith/main_public.runfiles/_main/projects/monolith/.main_public/bin/python3",
        "-m",
        "artifact.publish_public_assets",
        "/work/immutable",
    ]


def test_writer_stays_in_existing_job_namespace_without_api_authority(rendered):
    pod = publisher(rendered)["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "default"
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 65532
    assert pod["initContainers"][0].get("env", []) == []
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert env["ARTIFACTS_S3_BUCKET"]["value"] == "artifacts"
    assert env["AWS_EC2_METADATA_DISABLED"]["value"] == "true"
    for name, key in (
        ("S3_ACCESS_KEY_ID", "access-key-id"),
        ("S3_SECRET_ACCESS_KEY", "secret-access-key"),
    ):
        assert env[name]["valueFrom"]["secretKeyRef"] == {
            "name": "monolith-r2-s3",
            "key": key,
        }
    assert not {"DATABASE_URL", "GITHUB_TOKEN"} & env.keys()
    assert not any(
        doc["kind"] in {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
        for doc in rendered
    )
    assert pod["volumes"] == [
        {"name": "assets", "emptyDir": {"sizeLimit": "128Mi"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}},
    ]
    assert pod["containers"][0]["volumeMounts"][0]["readOnly"] is True
    for container in pod["initContainers"] + pod["containers"]:
        assert container["securityContext"] == {
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        }
        assert container["resources"]["limits"]["memory"]


def test_public_runtime_keeps_only_reader_and_frontend_transport(rendered):
    frontend = named(rendered, "Deployment", "monolith-public-frontend")
    web = named(rendered, "Deployment", "monolith-public-web")
    front_env = {
        item["name"]: item
        for item in frontend["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    web_env = {
        item["name"]: item
        for item in web["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert front_env["PUBLIC_ASSET_ARCHIVE"]["value"] == "true"
    assert not {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"} & front_env.keys()
    for name in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        assert (
            web_env[name]["valueFrom"]["secretKeyRef"]["name"]
            == "monolith-public-r2-s3"
        )


def copy(rendered, source, destination):
    container = publisher(rendered)["spec"]["template"]["spec"]["initContainers"][0]
    return subprocess.run(
        [
            os.environ.get("NODE_BIN", "node"),
            *container["command"][1:],
            container["args"][0],
            str(source),
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_exact_copy_keeps_representations_and_excludes_sibling_code(rendered, tmp_path):
    source = tmp_path / "client" / "_app" / "immutable"
    (source / "entry").mkdir(parents=True)
    (source / "workers").mkdir()
    expected = {
        "entry/start.abcdefgh.js": b"public-entry",
        "entry/start.abcdefgh.js.br": b"brotli-representation",
        "entry/start.abcdefgh.js.gz": b"gzip-representation",
        "workers/map.abcdefgh.js": b"public-worker",
    }
    for key, body in expected.items():
        (source / key).write_bytes(body)
    (source.parent / "version.json").write_text("mutable")
    (tmp_path / "server.js").write_text("server-secret")
    output = tmp_path / "output"
    result = copy(rendered, source, output)
    assert result.returncode == 0, result.stderr
    assert {
        str(path.relative_to(output)): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    } == expected
    assert copy(rendered, source, output).returncode != 0


@pytest.mark.parametrize(
    "bad",
    [
        "empty",
        "linked-root",
        "linked-file",
        "linked-directory",
        "oversized",
        "total-size",
        "too-many",
        "invalid-name",
    ],
)
def test_copy_rejects_unsafe_or_unbounded_input_before_writing(rendered, tmp_path, bad):
    source = tmp_path / "immutable"
    source.mkdir()
    if bad != "empty":
        (source / "start.abcdefgh.js").write_text("public")
    if bad == "linked-root":
        link = tmp_path / "link"
        link.symlink_to(source, target_is_directory=True)
        source = link
    elif bad.startswith("linked-"):
        (source / "linked").symlink_to(
            tmp_path if bad == "linked-directory" else source / "start.abcdefgh.js"
        )
    elif bad == "oversized":
        with (source / "large.js").open("wb") as f:
            f.truncate(16 * 1024 * 1024 + 1)
    elif bad == "total-size":
        for index in range(4):
            with (source / f"large{index}.js").open("wb") as f:
                f.truncate(16 * 1024 * 1024)
    elif bad == "too-many":
        for index in range(4096):
            (source / f"asset{index}.js").touch()
    elif bad == "invalid-name":
        (source / "space name.js").touch()
    output = tmp_path / "output"
    assert copy(rendered, source, output).returncode != 0
    assert not output.exists()
