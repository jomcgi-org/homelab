"""S6's chart wiring stays inert until conformance.s6.enabled flips (#6415).

With the key off, the conformance Deployment must render exactly as it did
before S6 existed: no TLC env, no /tmp volume, the S1..S5 resources. With it
on, the runner needs three things the S1..S5 pod never did, or S6 can only
ever read vacuous or incomplete: the S6_TLC_* env pointing at the toolchain
the image ships under /opt/tla, a writable /tmp under the read-only root
filesystem (the runner's os.MkdirTemp work dir, and TLC's extracted standard
modules), and a memory limit that hosts a JVM.

The in-image paths are read from the two files that define the layer, so a
path moved there without the chart fails here rather than in the dev cluster.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_RUNNER = "embervm-dev-embervm-conformance"
_TLC_ENV = ("S6_TLC_JAVA", "S6_TLC_JAR", "S6_TLC_SPEC_DIR", "JAVA_TOOL_OPTIONS")


def _chart_dir() -> Path:
    return Path(__file__).resolve().parent


def _helm(release: str, values: list[Path], settings: list[str]):
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(_chart_dir()),
        "--namespace",
        release,
    ]
    for path in values:
        argv += ["--values", str(path)]
    for setting in settings:
        argv += ["--set", setting]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _render(release: str, values: list[Path], settings: list[str] | None = None):
    result = _helm(release, values, settings or [])
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _runner_pod(rendered: str) -> dict | None:
    for doc in yaml.safe_load_all(rendered):
        if (
            isinstance(doc, dict)
            and doc.get("kind") == "Deployment"
            and doc["metadata"]["name"] == _RUNNER
        ):
            return doc["spec"]["template"]["spec"]
    return None


def _env(container: dict) -> dict[str, str]:
    return {item["name"]: item["value"] for item in container["env"]}


def _chart_values() -> dict:
    return yaml.safe_load((_chart_dir() / "values.yaml").read_text())


def _dev_values() -> list[Path]:
    return [_chart_dir() / "values.yaml", Path(os.environ["DEV_VALUES"])]


# Every committed overlay that renders the runner: dev alone, and dev on the
# GKE hub.
_DEV_OVERLAYS = {
    "dev": [],
    "dev-gke": ["DEV_GKE_VALUES"],
}


def _image_layout() -> dict[str, str]:
    """The /opt/tla paths, read from the layer's mtree script and image test."""
    image_test = Path(os.environ["S6_IMAGE_TEST"]).read_text()
    consts = dict(
        re.findall(r'(s6TLCImage(?:Java|Jar|SpecDir))\s*=\s*"([^"]+)"', image_test)
    )
    layout = {
        "S6_TLC_JAVA": "/" + consts["s6TLCImageJava"],
        "S6_TLC_JAR": "/" + consts["s6TLCImageJar"],
        "S6_TLC_SPEC_DIR": "/" + consts["s6TLCImageSpecDir"],
    }
    mtree = Path(os.environ["S6_LAYER_MTREE"]).read_text()
    # The script writes the JRE under ./opt/tla/jre and the jar and spec at
    # fixed entries; the chart's three paths must be exactly those.
    assert '"./opt/tla/jre/$rel"' in mtree
    assert layout["S6_TLC_JAVA"] == "/opt/tla/jre/bin/java"
    assert f'".{layout["S6_TLC_JAR"]} type=file' in mtree
    assert f'".{layout["S6_TLC_SPEC_DIR"]}/adoption_trace.tla type=file' in mtree
    return layout


@pytest.mark.parametrize("overlay", sorted(_DEV_OVERLAYS))
def test_s6_off_renders_no_tlc_wiring(overlay: str) -> None:
    extra = [Path(os.environ[key]) for key in _DEV_OVERLAYS[overlay]]
    pod = _runner_pod(_render("embervm-dev", _dev_values() + extra))
    assert pod is not None, "dev renders the conformance runner"
    (container,) = pod["containers"]
    env = _env(container)

    assert env["S6_ENABLED"] == "false"
    assert not set(_TLC_ENV) & set(env)
    assert "volumes" not in pod
    assert "volumeMounts" not in container
    assert container["resources"] == _chart_values()["conformance"]["resources"]


def test_s6_on_wires_toolchain_tmp_and_jvm_memory() -> None:
    off = _runner_pod(_render("embervm-dev", _dev_values()))
    on = _runner_pod(
        _render("embervm-dev", _dev_values(), ["conformance.s6.enabled=true"])
    )
    s6 = _chart_values()["conformance"]["s6"]
    (container,) = on["containers"]
    env = _env(container)

    assert env["S6_ENABLED"] == "true"
    for name, path in _image_layout().items():
        assert env[name] == path
    assert env["JAVA_TOOL_OPTIONS"] == s6["tlc"]["javaToolOptions"]
    assert "-XX:ActiveProcessorCount=2" in env["JAVA_TOOL_OPTIONS"]

    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
    assert on["volumes"] == [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]

    security = container["securityContext"]
    assert security["readOnlyRootFilesystem"] is True
    assert security["runAsNonRoot"] is True
    assert security["runAsUser"] == 65532
    assert security["allowPrivilegeEscalation"] is False

    assert container["resources"] == s6["resources"]
    assert container["resources"]["requests"]["memory"] == "256Mi"
    assert container["resources"]["limits"] == {"memory": "768Mi"}
    assert "cpu" not in container["resources"]["limits"]

    # Nothing else moves: the same container once the S6 deltas are removed.
    stripped = dict(container)
    stripped.pop("volumeMounts")
    stripped["resources"] = off["containers"][0]["resources"]
    stripped["env"] = [
        item
        for item in container["env"]
        if item["name"] not in _TLC_ENV and item["name"] != "S6_ENABLED"
    ]
    baseline = dict(off["containers"][0])
    baseline["env"] = [item for item in baseline["env"] if item["name"] != "S6_ENABLED"]
    assert stripped == baseline


def test_s6_on_tolerates_null_optional_keys() -> None:
    rendered = _render(
        "embervm-dev",
        _dev_values(),
        [
            "conformance.s6.enabled=true",
            "conformance.s6.tlc=null",
            "conformance.s6.tmpSizeLimit=null",
        ],
    )
    assert "<nil>" not in rendered
    pod = _runner_pod(rendered)
    (container,) = pod["containers"]
    env = _env(container)
    for name, path in _image_layout().items():
        assert env[name] == path
    assert "JAVA_TOOL_OPTIONS" not in env
    assert pod["volumes"][0]["emptyDir"] == {"sizeLimit": "64Mi"}


def test_s6_on_refuses_to_render_without_s6_resources() -> None:
    result = _helm(
        "embervm-dev",
        _dev_values(),
        ["conformance.s6.enabled=true", "conformance.s6.resources=null"],
    )
    assert result.returncode != 0
    assert "conformance.s6.resources is required" in result.stderr


@pytest.mark.parametrize(
    "settings", [[], ["conformance.s6.enabled=true"]], ids=["committed", "s6-on"]
)
def test_recovery_preset_renders_no_runner(settings: list[str]) -> None:
    rendered = _render(
        "embervm-dev",
        [_chart_dir() / "values.yaml", Path(os.environ["RECOVERY_VALUES"])],
        settings,
    )
    assert _runner_pod(rendered) is None
    assert "S6_TLC_" not in rendered


def test_production_namespace_refuses_runner_with_s6_on() -> None:
    result = _helm(
        "embervm",
        [_chart_dir() / "values.yaml", Path(os.environ["PROD_VALUES"])],
        ["conformance.enabled=true", "conformance.s6.enabled=true"],
    )
    assert result.returncode != 0
    assert "conformance runner is dev-only" in result.stderr


def test_s6_stays_off_in_every_committed_values_file() -> None:
    assert _chart_values()["conformance"]["s6"]["enabled"] is False
    for key in ("DEV_VALUES", "RECOVERY_VALUES"):
        values = yaml.safe_load(Path(os.environ[key]).read_text())
        assert values["conformance"]["s6"]["enabled"] is False
    for key in ("PROD_VALUES", "DEV_GKE_VALUES"):
        values = yaml.safe_load(Path(os.environ[key]).read_text()) or {}
        s6 = (values.get("conformance") or {}).get("s6") or {}
        assert s6.get("enabled", False) is False
