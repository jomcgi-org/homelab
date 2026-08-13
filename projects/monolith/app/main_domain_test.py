"""Smoke-compose every domain standalone (the per-domain image entrypoint path).

For each name in ``DOMAIN_NAMES`` this composes ``build_app(domain_profile(n),
[MODULE])`` exactly as ``app/main_domain.py`` does inside the per-domain
images, asserting the app builds and mounts its surface. Also guards the
parity between the Python registry (``app/modules_private.py``) and the Bazel
image fan-out list (``domain_images.bzl``).
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import re
import sys
from pathlib import Path

import pytest

# Ensure no valid static directory interferes with composition.
os.environ.pop("STATIC_DIR", None)

from fastapi.routing import iter_route_contexts  # noqa: E402

from app.modules_private import ALL_MODULES, DOMAIN_NAMES  # noqa: E402
from framework import Module, build_app, domain_profile  # noqa: E402


# OTel setup is process-global and already covered by main_otel_test; keep the
# smoke compositions hermetic.
def _profile(name: str):
    return dataclasses.replace(domain_profile(name), otel_enabled=False)


def _route_paths(app) -> set[str]:
    """Effective route paths, resolving FastAPI's lazy include_router entries.

    Since FastAPI 0.138 ``include_router`` appends an ``_IncludedRouter``
    placeholder (no ``.path``) instead of flattened routes, so iterating
    ``app.routes`` directly no longer sees a domain's included routes.
    """
    return {ctx.path for ctx in iter_route_contexts(app.routes)}


@pytest.mark.parametrize("name", DOMAIN_NAMES)
def test_domain_composes_standalone(name: str) -> None:
    module = importlib.import_module(name + ".module").MODULE
    assert isinstance(module, Module)
    assert module.name == name

    app = build_app(_profile(name), [module])
    paths = _route_paths(app)
    assert "/healthz" in paths
    assert "/api/health" in paths
    if module.register is not None:
        # A routed domain must contribute at least one route beyond the
        # framework's own surface (health, docs/openapi defaults, the MCP
        # mount): without /mcp in this set a routeless domain would pass
        # vacuously through the framework-mounted /mcp path.
        framework_paths = {
            "/healthz",
            "/api/health",
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/mcp",
        }
        assert paths - framework_paths, f"{name} mounted no routes of its own"


def test_registry_matches_module_names() -> None:
    assert DOMAIN_NAMES == tuple(m.name for m in ALL_MODULES)


def test_bazel_domain_list_matches_registry() -> None:
    """MONOLITH_DOMAINS in domain_images.bzl must mirror DOMAIN_NAMES."""
    bzl = Path(__file__).resolve().parent.parent / "domain_images.bzl"
    text = bzl.read_text()
    match = re.search(r"MONOLITH_DOMAINS = \[(.*?)\]", text, re.DOTALL)
    assert match, "MONOLITH_DOMAINS list not found in domain_images.bzl"
    bazel_domains = tuple(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert bazel_domains == DOMAIN_NAMES


def test_main_domain_entrypoint_requires_env(monkeypatch) -> None:
    monkeypatch.delenv("MONOLITH_DOMAIN", raising=False)
    sys.modules.pop("app.main_domain", None)
    with pytest.raises(RuntimeError, match="MONOLITH_DOMAIN"):
        importlib.import_module("app.main_domain")


def test_main_domain_entrypoint_composes_selected_domain(monkeypatch) -> None:
    monkeypatch.setenv("MONOLITH_DOMAIN", "hikes")
    sys.modules.pop("app.main_domain", None)
    mod = importlib.import_module("app.main_domain")
    paths = _route_paths(mod.app)
    assert any(p.startswith("/api/hikes") for p in paths)
    sys.modules.pop("app.main_domain", None)


def test_private_profile_serves_deep_health():
    """The confined monolith must mount /api/health.

    It used to opt out, which made every private-tier register_health component
    dead code: the component composes fine and the endpoint that would run it
    does not exist. The cd component (#4599) shipped that way and never ran.
    """
    from framework import PRIVATE_PROFILE

    # _add_health returns before registering /api/health when this is False,
    # so the flag IS the route's presence. Asserted on the profile rather than
    # by composing the app, which needs the full private dependency set.
    assert PRIVATE_PROFILE.deep_health is True


def test_cd_component_is_registered_on_the_private_tier():
    """Guards the join that broke: component registered, endpoint present."""
    from app.modules_private import ALL_MODULES

    names = {
        n
        for m in ALL_MODULES
        for checks in (m.register_health, m.register_health_advisory)
        if checks
        for n in checks
    }
    assert "cd" in names
