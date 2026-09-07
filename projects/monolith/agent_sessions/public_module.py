"""Public-only FastMonolith module for aggregate agent activity."""

from framework import Module as _Module


def register_public(app) -> None:
    from agent_sessions.public_router import router

    app.include_router(router)


MODULE = _Module(
    name="agent_activity",
    register_public=register_public,
)
