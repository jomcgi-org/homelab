"""Tests for stars router registration."""

from unittest.mock import MagicMock


def test_register_includes_stars_router():
    """register() calls app.include_router once with the stars router."""
    import stars

    app = MagicMock()
    stars.register(app)
    app.include_router.assert_called_once()


def test_register_passes_router_to_include_router():
    """register() passes the stars.router.router object to include_router."""
    import stars
    from stars.router import router as real_router

    app = MagicMock()
    stars.register(app)
    args, _ = app.include_router.call_args
    assert args[0] is real_router
