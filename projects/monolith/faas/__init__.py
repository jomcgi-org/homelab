"""FaaS domain: register-with-test-run function ingestion on EmberVM (ADR 045).

Private-only in R1 (Task 10): the ingestion API is an authenticated author
surface, so there is no ``register_public``. The public-tier invocation router
is a later, checklist-gated PR (Task 13).
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Mount the private faas routers: the ingestion API and the invocation router.

    Both are private-only in R1: there is no ``register_public``. The invocation
    router serves the ``/functions/<name>`` product URL surface (Task 11); the
    ingestion API (``/api/functions``) is the authenticated author surface (Task
    10). The public-tier invocation router is a later, checklist-gated PR.
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
