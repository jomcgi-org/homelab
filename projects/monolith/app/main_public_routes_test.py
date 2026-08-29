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


def _iter_route_paths(routes) -> "list[str]":
    """Yield every route ``.path``, recursing into included/mounted sub-routers.

    Starlette 1.x no longer flattens ``app.include_router()`` output into
    ``app.routes``: each included router appears as a single ``_IncludedRouter``
    wrapper with no ``.path``, and its child ``APIRoute`` objects (which carry
    the full, prefix-resolved ``.path``) live on ``wrapper.original_router.routes``.
    Recursing into included routers is what makes the deny-by-default allowlist
    below see the complete route surface (a partial walk would let a private
    path silently pass the leak checks). ``Mount`` sub-apps yield their own
    mount path (e.g. ``/mcp``, which ``test_no_mcp_mount`` asserts against) but
    are NOT descended into: their internal routes are un-prefixed and out of
    scope, matching the old flattened 0.x behaviour.
    """
    from starlette.routing import Mount

    out: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            out.append(path)
        if isinstance(route, Mount):
            continue
        sub = getattr(route, "routes", None)
        if sub is None:
            orig = getattr(route, "original_router", None)
            sub = getattr(orig, "routes", None) if orig is not None else None
        if sub:
            out.extend(_iter_route_paths(sub))
    return out


def _paths() -> set[str]:
    return set(_iter_route_paths(app.routes))


# ---------------------------------------------------------------------------
# Positive assertions: these public paths MUST be present.
# ---------------------------------------------------------------------------

REQUIRED_PATHS = [
    "/api/knowledge/public/graph",
    "/api/knowledge/public/notes/{note_id}",
    "/api/home/observability/stats",
    "/api/ember/postgres/status",
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
    "/api/grimoire",
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


def test_public_faas_invocation_mounted():
    # The public FaaS product surface (Task 13) must be mounted, and only the
    # invocation router (no /api/functions ingestion on the public tier).
    paths = _paths()
    assert any(p.startswith("/functions") for p in paths), (
        "expected the public /functions/<name> invocation router to be mounted"
    )
    assert not any(p.startswith("/api/functions") for p in paths), (
        "the ingestion API (/api/functions) must NOT be mounted on the public tier"
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
    # Grimoire public tier (public-readonly design):
    # no campaign/grant params, whole corpus is a single global read view.
    "/api/grimoire",
    # Internal-only public chat API. It is mounted on the public binary but is
    # deliberately kept off the public HTTPRoute (see
    # projects/monolith-public/chart/httproute_public_test.py): it is reachable
    # only in-cluster from the SSR front door (Cilium datapath), never directly
    # from the internet.
    "/internal/chat",
    # Internal-only grimoire D&D chat API (mirrors /internal/chat, ADR 005):
    # mounted on the public binary, reachable only in-cluster from the SSR front
    # door (Cilium datapath), kept off the public HTTPRoute.
    "/internal/grimoire-chat",
    # Internal-only artifact read API (ADR 024): mounted on the public binary so
    # the SSR frontend can proxy /artifact/<id>/raw + /version in-cluster, but
    # kept off the public HTTPRoute (the frontend is the sole public origin). The
    # write router is NOT mounted here (the public tier stays read-only).
    "/internal/artifact",
    # Public FaaS invocation surface (Task 13): jomcgi.dev/functions/<name> for
    # visibility=public functions only. This is the ONE public route that is not
    # under /api (it is the product URL, per ADR agents/045); the ingestion API
    # (/api/functions) is deliberately NOT mounted on the public tier (register vs
    # register_public). The router filters visibility=public, so a private
    # function 404s here (faas/invoke_router_public_test.py asserts it).
    "/functions",
    # Ember public tier (public pages design): the
    # scale-to-zero demo-postgres exhibit, Turnstile-gated when public,
    # mounted identically on the private tier's demos panel.
    "/api/ember",
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
        "/api/updates",
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
