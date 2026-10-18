"""FaaS domain: register-with-test-run function ingestion on EmberVM (ADR 045).

The private tier mounts the authenticated ingestion API and invocation router.
The public tier mounts only invocation for smoke-passed, public functions.
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Mount the private faas routers: the ingestion API and the invocation router.

    The invocation router serves ``/functions/<name>`` for smoke-passed functions.
    The ingestion API at ``/api/functions`` is the authenticated author surface.
    """
    from faas.invoke_router import router as invoke_router
    from faas.router import router

    app.include_router(router)
    app.include_router(invoke_router)


def register_public(app: FastAPI) -> None:
    """Mount ONLY the public-tier invocation router (Task 13).

    The public origin serves ``/functions/<name>`` for ``visibility=public``
    functions only (``invoke_router_public``). It deliberately does NOT mount the
    ingestion API (``/api/functions``): registration is the authenticated
    private-tier author surface (standing decision 7), and the public tier has no
    ``/api`` ingress at all (public-tier checklist item 2). This closure must not
    import ``faas.router``/``faas.storage``/``faas.workload`` (the private write
    path); ``main_public_imports_test`` enforces that.
    """
    from faas.invoke_router_public import router as invoke_public_router

    app.include_router(invoke_public_router)
