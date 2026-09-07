"""FastMonolith module export for observability routes."""

from framework import Module as _Module


def register(app) -> None:
    """Register the merged pull request router with the app."""
    from observability import public_router

    app.include_router(public_router.router)


MODULE = _Module(
    name="observability",
    register=register,
    register_public=register,
)
