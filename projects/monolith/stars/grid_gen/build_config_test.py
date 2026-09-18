"""Static checks for the grid image's dependency and runtime isolation."""

import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
MONOLITH = HERE.parents[1]


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_geospatial_dependencies_are_only_on_dedicated_binary():
    build = (MONOLITH / "BUILD").read_text()
    backend_sources = _between(build, "_BACKEND_SRCS = glob(", "py_venv_binary(")
    stars_package = _between(build, 'name = "pkg_stars"', 'name = "pkg_chat_public"')
    monolith_backend = _between(build, 'name = "monolith_backend"', 'name = "image"')
    generator = _between(
        build,
        'name = "stars_grid_generator"',
        'name = "stars_grid_ingest"',
    )

    assert '"stars/grid_gen/**"' in backend_sources
    assert '"stars/grid_gen/**"' in stars_package
    assert '"stars/grid_gen/**"' in monolith_backend
    for dependency in ("geopandas", "rasterio", "shapely"):
        assert f'"@pip//{dependency}"' in generator
        assert build.count(f'"@pip//{dependency}"') == 1


def test_grid_job_has_an_explicit_minimal_runfiles_closure():
    build = (MONOLITH / "BUILD").read_text()
    job = _between(build, 'name = "stars_grid_job"', "# Progress-ingest entrypoint")

    assert 'srcs = ["stars/grid_gen/job.py"],  # keep' in job
    assert 'imports = ["."],  # keep' in job
    for dependency in (
        ":stars_grid_generator",
        ":stars_grid_ingest",
        "@pip//boto3",
        "@pip//botocore",
    ):
        assert f'"{dependency}"' in job
    assert "],  # keep" in job
    assert ":pkg_stars" not in job


def test_grid_runtime_is_dual_arch_and_non_root():
    config = (HERE / "apko.yaml").read_text()
    assert "  - x86_64\n  - aarch64\n" in config
    assert "uid: 65532" in config
    assert "gid: 65532" in config
    assert "run-as: 65532" in config

    build = (MONOLITH / "BUILD").read_text()
    image = _between(
        build,
        'name = "stars_grid_apko_base"',
        'name = "stars_grid_image"',
    )
    assert "arm64 = True" in image
    final_image = build.split('name = "stars_grid_image"', 1)[1]
    assert "multi_platform = True" in final_image


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
