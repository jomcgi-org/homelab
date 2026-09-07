"""Public observability read models and routes."""

from fastapi import FastAPI


def register_public(app: FastAPI) -> None:
    from observability.public_router import router

    app.include_router(router)
