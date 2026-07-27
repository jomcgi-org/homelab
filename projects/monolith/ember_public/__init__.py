"""Public-safe ember (embervm) domain: the scale-to-zero Postgres demo.

The demo-postgres core (control-plane status polling, the timed query
roundtrip, session minting, and the all-time sleep-savings counter) lives here
so it can be composed on BOTH the public tier (Turnstile-gated, jomcgi.dev/ember)
and the private tier (the authenticated demos panel), sharing one
implementation. This package must stay importable in the public closure: it
never imports ``demos``, ``sandbox.client``, or anything else on
``app/main_public_imports_test.py``'s forbidden list. ``EMBERVM_URL`` is read
directly from the environment here rather than imported from
``sandbox.client`` for that reason.

``demos/firecracker_api.py`` keeps the private-only ``POST /postgres/reset``
endpoint (destructive, griefing-sensitive) and imports what it needs from this
package; the private panel mounts this package's router alongside it so both
tiers serve identical paths.

This is a public-tier domain, so it exposes ``register_public`` (mounted by
``app/modules_public.py``); the private registry mounts the SAME router via
``register`` so the paths are identical on both tiers.
"""

from __future__ import annotations

from fastapi import FastAPI

__all__ = ["register", "register_public"]


def register_public(app: FastAPI) -> None:
    """Register the demo-postgres, bazel-query, and semgrep-scan routers on
    the public app."""
    from ember_public.bazel_router import router as bazel_router
    from ember_public.router import router
    from ember_public.semgrep_router import router as semgrep_router

    app.include_router(router)
    app.include_router(bazel_router)
    app.include_router(semgrep_router)


def register(app: FastAPI) -> None:
    """Register the demo-postgres, bazel-query, and semgrep-scan routers on
    the private app (same routers as the public app)."""
    from ember_public.bazel_router import router as bazel_router
    from ember_public.router import router
    from ember_public.semgrep_router import router as semgrep_router
    from ember_public.synthetic_router import internal_router

    app.include_router(router)
    app.include_router(bazel_router)
    app.include_router(semgrep_router)
    app.include_router(internal_router)
