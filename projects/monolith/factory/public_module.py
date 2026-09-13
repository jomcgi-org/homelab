"""Public-only FastMonolith module for published factory activity."""

from framework import Module as _Module


def register_public(app) -> None:
    from factory.public_view import router

    app.include_router(router)


MODULE = _Module(
    name="factory",
    register_public=register_public,
)
