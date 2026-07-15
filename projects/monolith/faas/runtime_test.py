"""Tests for the registration-time runtime contract (faas.runtime)."""

from __future__ import annotations

from faas.runtime import BAKED_PACKAGES, KNOWN_RUNTIMES


def test_baked_packages_covers_readme_subset():
    # Mirrors projects/embervm/runtimes/python/README.md "Baked dependency
    # subset": these importable top-level names must all be present.
    for pkg in ("pandas", "numpy", "matplotlib", "scipy", "PIL", "yaml", "dateutil"):
        assert pkg in BAKED_PACKAGES


def test_requests_is_not_baked():
    # A common declared dep that is NOT in the base: registration must reject it.
    assert "requests" not in BAKED_PACKAGES


def test_known_runtimes_is_python_only():
    assert KNOWN_RUNTIMES == frozenset({"python312"})
