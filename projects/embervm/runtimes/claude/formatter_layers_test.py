"""Execute the exact formatter layers shared by both source-writing guests."""

import json
import os
from pathlib import Path
import subprocess
import tarfile
import shutil

import pytest


@pytest.fixture
def layers(tmp_path):
    if "FORMATTER_TARS" not in os.environ:
        pytest.skip("requires Bazel-built runtime layers")
    root = tmp_path / "image"
    root.mkdir()
    for path in os.environ["FORMATTER_TARS"].split():
        runfile = Path(os.environ["TEST_SRCDIR"]) / "_main" / path
        with tarfile.open(runfile) as archive:
            archive.extractall(root, filter="data")
    return root


def run(command, cwd, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_packaged_formatters_and_svelte_plugin_execute(layers, tmp_path):
    binaries = layers / "usr/bin"
    for name, flag in [
        ("ruff", "--version"),
        ("shfmt", "--version"),
        ("gofumpt", "--version"),
        ("buildifier", "--version"),
        ("helm", "version"),
    ]:
        run([str(binaries / name), flag], tmp_path)
    source = tmp_path / "component.svelte"
    source.write_text("<script>let x=1;</script><p>{x}</p>")
    # Use the production config and wrapper's NODE_PATH, with no --plugin.
    config = tmp_path / "prettier.config.cjs"
    shutil.copyfile(repo_file("bazel/tools/format/prettier.config.cjs"), config)
    run(
        [str(binaries / "prettier"), "--config", str(config), "--write", str(source)],
        tmp_path,
    )
    assert "let x = 1;" in source.read_text()
    # This layer overlays apko's helm-3 package. Test the effective binary.
    assert (
        "v3.16.4"
        in run([str(binaries / "helm"), "version", "--short"], tmp_path).stdout
    )


def repo_file(path):
    return Path(os.environ["TEST_SRCDIR"]) / "_main" / path


def test_packaged_repository_gazelle_generates_go_build(layers, tmp_path):
    generator = layers / "usr/local/libexec/homelab-gazelle"
    manifest = json.loads((generator.parent / "gazelle-inputs.json").read_text())
    assert {
        "BUILD",
        "MODULE.bazel",
        "go.mod",
        "go.sum",
        "bazel/helm/gazelle/generate.go",
    } <= set(manifest)
    fixture = tmp_path / "repo"
    fixture.mkdir()
    for name in ("BUILD", "MODULE.bazel", "go.mod", "go.sum"):
        shutil.copyfile(repo_file(name), fixture / name)
    plugin_dir = fixture / "bazel/helm/gazelle"
    plugin_dir.mkdir(parents=True)
    for path in repo_file("bazel/helm/gazelle").iterdir():
        if path.suffix == ".go" or path.name == "BUILD":
            shutil.copyfile(path, plugin_dir / path.name)
    original = (plugin_dir / "BUILD").read_text()
    sample = fixture / "sample"
    sample.mkdir()
    (sample / "hello.go").write_text(
        "package hello\nimport (\n"
        '_ "github.com/spiffe/go-spiffe/v2/spiffeid"\n'
        '_ "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"\n)\n'
    )
    go = repo_file(os.environ["GO_RUNNER"])
    env = dict(os.environ, GOPROXY="off", GOSUMDB="off", GOTOOLCHAIN="local")
    env["GOROOT"] = run([str(go), "env", "GOROOT"], fixture, env).stdout.strip()
    run(
        [
            str(generator),
            "-repo_root",
            str(fixture),
            "-bzlmod",
            "-repo_config",
            str(generator.parent / "gazelle-repo-config"),
            "-lang=go,proto",
            "-external=static",
        ],
        fixture,
        env,
    )
    assert (plugin_dir / "BUILD").read_text() == original
    build_files = [
        path for name in ("BUILD", "BUILD.bazel") if (path := sample / name).is_file()
    ]
    assert len(build_files) == 1
    generated = build_files[0].read_text()
    assert "@com_github_spiffe_go_spiffe_v2//spiffeid" in generated
    assert "@com_github_envoyproxy_go_control_plane_envoy//config/core/v3" in generated
    assert "@bazel_gazelle" not in generated
