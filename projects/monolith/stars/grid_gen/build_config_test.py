"""Static checks for the grid image's dependency and runtime isolation."""

import ast
import json
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
MONOLITH = HERE.parents[1]


def _named_target(text: str, name: str) -> str:
    """Return a top-level BUILD call identified by its exact target name."""
    for match in re.finditer(
        r"(?ms)^[a-z_][a-z0-9_]*\(\n.*?^\)\n",
        text,
    ):
        target = match.group(0)
        if re.search(rf'^    name = "{re.escape(name)}",$', target, re.MULTILINE):
            return target
    raise AssertionError(f"BUILD target {name!r} not found")


def _assignment_call(text: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(name)} = [a-z_][a-z0-9_]*\(\n.*?^\)\n",
        text,
    )
    assert match is not None, f"BUILD assignment {name!r} not found"
    return match.group(0)


def _list_attr(target: str, name: str) -> set[str]:
    match = re.search(
        rf"(?ms)^    {re.escape(name)} = \[\n(?P<body>.*?)^    \],(?:  # keep)?$",
        target,
    )
    assert match is not None, f"list attribute {name!r} not found"
    return set(re.findall(r'^        "([^"]+)",$', match.group("body"), re.MULTILINE))


def test_raster_dependencies_stay_dedicated_and_point_geometry_is_explicit():
    build = (MONOLITH / "BUILD").read_text()
    backend_sources = _assignment_call(build, "_BACKEND_SRCS")
    stars_package = _named_target(build, "pkg_stars")
    monolith_backend = _named_target(build, "monolith_backend")
    generator = _named_target(build, "stars_grid_generator")

    assert '"stars/grid_gen/**"' in backend_sources
    assert '"stars/grid_gen/**"' in stars_package
    assert '"stars/grid_gen/**"' in monolith_backend
    for dependency in ("geopandas", "rasterio"):
        assert f'"@pip//{dependency}"' in generator
        assert build.count(f'"@pip//{dependency}"') == 1

    # Camping materialization uses GEOS point coverage for holes, islands and
    # edges. Name every permitted consumer instead of allowing GIS wheels to
    # spread into unrelated package deps. Raster ingestion stays grid-only.
    shared = _named_target(build, "pkg_shared")
    camping_test = _named_target(build, "shared_camping_test")
    for target in (generator, shared, monolith_backend, camping_test):
        assert '"@pip//shapely"' in target
    assert build.count('"@pip//shapely"') == 4
    assert 'data = ["shared/camping_boundaries.json.gz"]' in shared
    assert '"shared/camping_boundaries.json.gz"' in monolith_backend


def test_grid_job_has_an_explicit_minimal_runfiles_closure():
    build = (MONOLITH / "BUILD").read_text()
    job = _named_target(build, "stars_grid_job")

    assert 'srcs = ["stars/grid_gen/job.py"],  # keep' in job
    assert 'imports = ["."],  # keep' in job
    assert _list_attr(job, "deps") == {
        ":stars_grid_generator",
        ":stars_grid_ingest",
        "@pip//boto3",
        "@pip//botocore",
        "@pip//opentelemetry_api",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_sdk",
    }
    assert ":pkg_stars" not in job


def test_grid_runtime_image_is_amd64_only_and_non_root():
    config = (HERE / "apko.yaml").read_text()
    assert "  - x86_64\n  - aarch64\n" in config
    assert "uid: 65532" in config
    assert "gid: 65532" in config
    assert "run-as: 65532" in config

    build = (MONOLITH / "BUILD").read_text()
    image = _named_target(build, "stars_grid_apko_base")
    assert "arm64 = False" in image
    final_image = _named_target(build, "stars_grid_image")
    assert 'base = ":stars_grid_apko_base"' in final_image
    assert final_image.startswith("py3_image(")
    assert "bash_symlink = False" in final_image

    # Verify the wrapper's surviving path: one image inheriting the apko user,
    # with the public image and its digest both selecting Linux AMD64 in CI.
    macro = ast.parse(
        (MONOLITH.parents[1] / "bazel/tools/oci/py3_image.bzl").read_text()
    )
    calls = [
        node
        for node in ast.walk(macro)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    images = [call for call in calls if call.func.id == "oci_image"]
    assert len(images) == 1
    image_attrs = {kw.arg: kw.value for kw in images[0].keywords}
    assert isinstance(image_attrs["base"], ast.Name)
    assert image_attrs["base"].id == "base"
    assert "user" not in image_attrs  # Preserve uid 65532 from the custom base.
    assert not any(call.func.id == "oci_image_index" for call in calls)
    transitions = [
        call for call in calls if call.func.id == "platform_transition_filegroup"
    ]
    assert len(transitions) == 2
    for transition in transitions:
        attrs = {kw.arg: kw.value for kw in transition.keywords}
        selection = attrs["target_platform"]
        assert isinstance(selection, ast.Call)
        assert isinstance(selection.func, ast.Name)
        assert selection.func.id == "select"
        platforms = ast.literal_eval(selection.args[0])
        assert platforms["@platforms//cpu:x86_64"] == (
            "//bazel/tools/platforms:linux_x86_64"
        )


def test_grid_runtime_contains_native_cpp_library_on_both_architectures():
    config = (HERE / "apko.yaml").read_text()
    assert "    - libstdc++\n" in config

    lock = json.loads((HERE / "apko.lock.json").read_text())
    packages = lock["contents"]["packages"]
    for architecture in ("x86_64", "aarch64"):
        names = {
            package["name"]
            for package in packages
            if package["architecture"] == architecture
        }
        assert "libstdc++" in names
