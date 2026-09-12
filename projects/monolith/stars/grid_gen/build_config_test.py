"""Static checks for the grid image's dependency and runtime isolation."""

from pathlib import Path


HERE = Path(__file__).resolve().parent
MONOLITH = HERE.parents[1]


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_geospatial_dependencies_are_only_on_dedicated_binary():
    build = (MONOLITH / "BUILD").read_text()
    backend_sources = _between(build, "_BACKEND_SRCS = glob(", "py_venv_binary(")
    generator = _between(
        build,
        'name = "stars_grid_generator"',
        'name = "stars_grid_job"',
    )

    assert '"stars/grid_gen/**"' in backend_sources
    for dependency in ("geopandas", "rasterio", "shapely"):
        assert f'"@pip//{dependency}"' in generator
        assert f'"@pip//{dependency}"' not in backend_sources


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
