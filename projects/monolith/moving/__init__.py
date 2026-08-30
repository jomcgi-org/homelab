"""Moving: private in-monolith two-person move planner."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register moving routers with the private app."""
    from moving.chat import router as chat_router
    from moving.router import router

    app.include_router(router)
    app.include_router(chat_router)
