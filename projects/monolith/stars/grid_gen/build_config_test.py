"""Static checks for the grid image's dependency and runtime isolation."""

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


def test_geospatial_dependencies_are_only_on_dedicated_binary():
    build = (MONOLITH / "BUILD").read_text()
    backend_sources = _assignment_call(build, "_BACKEND_SRCS")
    stars_package = _named_target(build, "pkg_stars")
    monolith_backend = _named_target(build, "monolith_backend")
    generator = _named_target(build, "stars_grid_generator")

    assert '"stars/grid_gen/**"' in backend_sources
    assert '"stars/grid_gen/**"' in stars_package
    assert '"stars/grid_gen/**"' in monolith_backend
    for dependency in ("geopandas", "rasterio", "shapely"):
        assert f'"@pip//{dependency}"' in generator
        assert build.count(f'"@pip//{dependency}"') == 1


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


def test_grid_runtime_is_dual_arch_and_non_root():
    config = (HERE / "apko.yaml").read_text()
    assert "  - x86_64\n  - aarch64\n" in config
    assert "uid: 65532" in config
    assert "gid: 65532" in config
    assert "run-as: 65532" in config

    build = (MONOLITH / "BUILD").read_text()
    image = _named_target(build, "stars_grid_apko_base")
    assert "arm64 = True" in image
    final_image = _named_target(build, "stars_grid_image")
    assert "multi_platform = True" in final_image

    architecture = (MONOLITH / "ARCHITECTURE.md").read_text()
    assert "Stars grid image architecture exception" in architecture
    assert "no current arm64 workload consumer" in architecture


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
