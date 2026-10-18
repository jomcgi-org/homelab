"""Tests for hikes router registration."""

from unittest.mock import MagicMock


def test_register_includes_hikes_router():
    """register() calls app.include_router once with the hikes router."""
    import hikes

    app = MagicMock()
    hikes.register(app)
    app.include_router.assert_called_once()


def test_register_passes_router_to_include_router():
    """register() passes the hikes.router.router object to include_router."""
    import hikes
    from hikes.router import router as real_router

    app = MagicMock()
    hikes.register(app)
    args, _ = app.include_router.call_args
    assert args[0] is real_router
