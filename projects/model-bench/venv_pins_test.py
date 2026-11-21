"""Check the verifier's exact pins without installing its environment."""

import re
from pathlib import Path

import pytest


@pytest.fixture
def pins():
    result = {}
    for line in (
        Path(__file__).with_name("requirements-venv.txt").read_text().splitlines()
    ):
        requirement = line.partition("#")[0].strip()
        if requirement:
            name, version = requirement.split("==")
            key = name.lower().replace("_", "-")
            assert key not in result, f"Duplicate pin for {key}"
            result[key] = version
    return result


def test_opentelemetry_family(pins):
    sdk = pins["opentelemetry-sdk"]
    assert pins["opentelemetry-api"] == sdk
    stable = re.fullmatch(r"1\.(\d+)\.(\d+)", sdk)
    assert stable, f"Expected a stable 1.x SDK version, got {sdk}"
    minor, patch = map(int, stable.groups())
    assert pins["opentelemetry-semantic-conventions"] == f"0.{minor + 21}b{patch}"


def test_pydantic_family(pins):
    # Snapshot Requires-Dist so PR tests work offline. Refresh from the new
    # release's PyPI metadata when updating Pydantic.
    required_core = {"2.13.4": "2.46.4"}
    version = pins["pydantic"]
    assert version in required_core, (
        f"Record pydantic-core from https://pypi.org/pypi/pydantic/{version}/json "
        "info.requires_dist"
    )
    assert pins["pydantic-core"] == required_core[version]
