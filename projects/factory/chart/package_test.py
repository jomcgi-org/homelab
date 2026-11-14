"""Prepare bundled values while ArgoCD keeps using the compatible git values."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parent
_APPLICATION = Path(
    os.environ.get("APPLICATION", _CHART.parent / "deploy/application.yaml")
)
_DEPLOY = Path(os.environ.get("DEPLOY_VALUES", _CHART.parent / "deploy/values.yaml"))
_LOCK = Path(os.environ.get("RENDER_LOCK", _CHART.parent / "deploy/render.sha256"))
_IMAGES = ("broker", "connector", "reviewer")


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    # Bazel supplies the actual helm_chart package, with build-time image pins.
    # A standalone run must also supply that artifact, never silently test a
    # separately constructed approximation of the publishing rule.
    archive = Path(os.environ["CHART_PACKAGE"])
    values = tmp_path_factory.mktemp("packaged-values")
    with tarfile.open(archive) as chart:
        for name in ("values.yaml", "values-homelab.yaml"):
            member = chart.extractfile(f"factory/{name}")
            assert member is not None, f"package is missing {name}"
            (values / name).write_bytes(member.read())
    return archive, values


def test_application_keeps_git_values_until_a_compatible_release_exists() -> None:
    # This preparation release must still render with the pre-bundle chart
    # pin, including when an operator lowers the pin after publication.
    # The cutover PR changes this contract only after a bundled release is
    # published and available as its previous-release rollback target.
    app = yaml.safe_load(_APPLICATION.read_text())
    source, git_values = app["spec"]["sources"]
    helm = source["helm"]
    assert source["repoURL"] == "ghcr.io/jomcgi/homelab/charts"
    assert source["chart"] == "factory"
    assert helm["valueFiles"] == ["$values/projects/factory/deploy/values.yaml"]
    assert helm["skipCrds"] is True
    assert set(helm) == {"releaseName", "skipCrds", "valueFiles"}
    assert git_values == {
        "repoURL": "https://github.com/jomcgi-org/homelab.git",
        "targetRevision": "HEAD",
        "ref": "values",
    }
    assert _DEPLOY.is_file()


def _render(chart: Path, values: Path, *overrides: str) -> str:
    app = yaml.safe_load(_APPLICATION.read_text())
    helm = app["spec"]["sources"][0]["helm"]
    return subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            helm["releaseName"],
            str(chart),
            "--namespace",
            app["spec"]["destination"]["namespace"],
            "--values",
            str(values),
            *overrides,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_homelab_values_are_packaged_without_becoming_defaults(
    package: tuple[Path, Path],
) -> None:
    _, values = package
    # Both paths remain checked in during preparation. This fails if a later
    # edit updates the live git values without updating the bundled copy.
    assert (values / "values-homelab.yaml").read_bytes() == _DEPLOY.read_bytes()
    expected = yaml.safe_load((_CHART / "values.yaml").read_text())
    published = yaml.safe_load((values / "values.yaml").read_text())
    for service in _IMAGES:
        image = published[service]["image"]
        assert image["repository"] == expected[service]["image"]["repository"]
        assert image["tag"]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"])
        for key in ("repository", "tag", "digest"):
            expected[service]["image"][key] = image[key]
    # The only changes to reusable defaults are the image pins from CI.
    assert published == expected


def test_packaged_render_matches_the_lock_with_published_images(
    package: tuple[Path, Path],
) -> None:
    archive, values = package
    source_render = _render(_CHART, _DEPLOY)
    normalized = re.sub(r"(helm\.sh/chart: factory-)\S+", r"\g<1>VERSION", source_render)
    assert hashlib.sha256(normalized.encode()).hexdigest() == _LOCK.read_text().strip()

    published = yaml.safe_load((values / "values.yaml").read_text())
    overrides = []
    for service in _IMAGES:
        overrides += [
            "--set-json",
            f"{service}.image={json.dumps(published[service]['image'])}",
        ]
    # Compare all rendered bytes, including ConfigMaps and hooks. Only CI's
    # generated image values may differ from the source render under the lock.
    rendered = _render(archive, values / "values-homelab.yaml")
    assert rendered == _render(_CHART, _DEPLOY, *overrides)
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
    for service in _IMAGES:
        (deployment,) = [
            doc
            for doc in docs
            if doc["kind"] == "Deployment"
            and doc["metadata"]["name"] == f"factory-{service}"
        ]
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        image = published[service]["image"]
        assert container["image"] == f"{image['repository']}@{image['digest']}"
