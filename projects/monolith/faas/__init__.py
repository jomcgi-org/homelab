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
