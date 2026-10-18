"""campsites: BC Parks availability x clear-sky weather (/app/campsites)."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Register the campsites router (read-only, public-safe)."""
    from campsites.router import router

    app.include_router(router)


register_public = register  # wholly public, read-only
