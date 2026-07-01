"""Route-table guard for the public app.

Asserts that ``app.main_public`` exposes ONLY the public, read-only route
surface and ZERO private paths. This is a pure import + introspection test:
it never starts the app or touches a database, so it stays fast and stable
in CI.

``import pytest`` is intentional even though no fixtures are used: it keeps
gazelle's dependency inference attaching ``@pip//pytest`` to this target.
"""

from __future__ import annotations

import pytest  # noqa: F401  (keeps the gazelle pytest dep; see module docstring)

from app.main_public import app


def _paths() -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


# ---------------------------------------------------------------------------
# Positive assertions: these public paths MUST be present.
# ---------------------------------------------------------------------------

REQUIRED_PATHS = [
    "/api/knowledge/public/graph",
    "/api/knowledge/public/notes/{note_id}",
    "/api/home/observability/stats",
    "/api/home/observability/topology",
]

# Prefixes for the wholly-public domains; at least one route per prefix must
# be mounted. Mirrors each domain router's declared prefix.
REQUIRED_PREFIXES = [
    "/api/ships",
    "/api/stars",
    "/api/hikes",
    "/api/dr-jobs",
    "/api/trips",
    "/api/campsites",
]


def test_required_public_paths_present():
    paths = _paths()
    for required in REQUIRED_PATHS:
        assert required in paths, f"expected public path {required!r} to be mounted"


def test_each_public_domain_has_a_route():
    paths = _paths()
    for prefix in REQUIRED_PREFIXES:
        assert any(p.startswith(prefix) for p in paths), (
            f"expected at least one route under {prefix!r}"
        )


def test_healthz_present():
    assert "/healthz" in _paths()


def test_api_health_present():
    assert "/api/health" in _paths()


# Deny-by-default allowlist: every route on the public app must fall under one
# of these prefixes. Unlike the negative assertions below (which forbid the
# private prefixes we know about today), this catches a future domain mounted
# under an unexpected prefix, so a new private surface cannot silently leak
# into the public artifact.
ALLOWED_PREFIXES = (
    "/api/ships",
    "/api/stars",
    "/api/hikes",
    "/api/dr-jobs",
    "/api/trips",
    # BC Parks campsite availability x clear-sky weather (read-only SSR snapshot).
    "/api/campsites",
    # SSR-only Scotland WC2026 qualification summary (kept off the public
    # HTTPRoute; reached only via the in-pod SSR fetch, never directly).
    "/api/wc2026",
    "/api/knowledge/public",
    "/api/home/observability",
    # Internal-only public chat API. It is mounted on the public binary but is
    # deliberately kept off the public HTTPRoute (see
    # projects/monolith-public/chart/httproute_public_test.py): it is reachable
    # only in-cluster from the SSR front door over Linkerd mTLS, never directly
    # from the internet.
    "/internal/chat",
    # Internal-only artifact read API (ADR 024): mounted on the public binary so
    # the SSR frontend can proxy /artifact/<id>/raw + /version in-cluster, but
    # kept off the public HTTPRoute (the frontend is the sole public origin). The
    # write router is NOT mounted here (the public tier stays read-only).
    "/internal/artifact",
    "/healthz",
    # Deep health probe (DB reachable + public_reader can query). Reached via the
    # frontend /health same-origin proxy; not a private surface.
    "/api/health",
    "/openapi.json",
    "/docs",
    "/redoc",
)


def test_only_allowed_route_prefixes():
    paths = _paths()
    for p in paths:
        assert p.startswith(ALLOWED_PREFIXES), (
            f"path {p!r} is not in the public allowlist; "
            "the public app must expose only public, read-only routes"
        )


# ---------------------------------------------------------------------------
# Negative assertions: these private paths MUST be absent.
# ---------------------------------------------------------------------------


def test_no_mcp_mount():
    paths = _paths()
    assert not any(p.startswith("/mcp") for p in paths), (
        "the public app must not mount /mcp"
    )


def test_only_public_knowledge_paths():
    paths = _paths()
    for p in paths:
        if p.startswith("/api/knowledge"):
            assert p.startswith("/api/knowledge/public"), (
                f"private knowledge path {p!r} leaked into the public app"
            )


def test_only_observability_home_paths():
    paths = _paths()
    for p in paths:
        if p.startswith("/api/home"):
            assert p.startswith("/api/home/observability"), (
                f"non-observability home path {p!r} leaked into the public app"
            )


def test_no_schedule_chat_scheduler_agent_paths():
    paths = _paths()
    forbidden_prefixes = [
        "/api/home/schedule",
        "/api/chat",
        "/api/scheduler",
        "/api/agent",
        # knowledge tasks router is private
        "/api/knowledge/tasks",
    ]
    for prefix in forbidden_prefixes:
        leaked = [p for p in paths if p.startswith(prefix)]
        assert not leaked, f"private paths under {prefix!r} leaked: {leaked}"


def test_specific_private_knowledge_paths_absent():
    paths = _paths()
    explicitly_absent = [
        "/api/knowledge/search",
        "/api/knowledge/graph",
        "/api/knowledge/notes/{note_id}",
        "/api/knowledge/gaps",
    ]
    for path in explicitly_absent:
        assert path not in paths, (
            f"private knowledge path {path!r} must not be in the public app"
        )
