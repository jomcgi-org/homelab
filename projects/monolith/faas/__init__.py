"""FaaS domain: register-with-test-run function ingestion on EmberVM (ADR 045).

Private-only in R1 (Task 10): the ingestion API is an authenticated author
surface, so there is no ``register_public``. The public-tier invocation router
is a later, checklist-gated PR (Task 13).
"""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Mount the private faas router (ingestion API)."""
    from faas.router import router

    app.include_router(router)
