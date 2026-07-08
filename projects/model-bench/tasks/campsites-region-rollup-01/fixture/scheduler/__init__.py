"""Scheduler domain — HTTP API for the shared Postgres-backed job scheduler."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from scheduler.router import router

    app.include_router(router)
