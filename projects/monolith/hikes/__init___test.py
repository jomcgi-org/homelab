"""Tests for hikes.__init__: router registration and startup job wiring."""

from unittest.mock import MagicMock, patch


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


def test_on_startup_jobs_calls_register_job_three_times():
    """on_startup_jobs() calls register_job exactly three times."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    assert mock_register.call_count == 3


def test_on_startup_jobs_correct_names():
    """on_startup_jobs() registers hikes.scrape_walks, hikes.refresh_forecasts, and hikes.prune_windows."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    names = {c[1]["name"] for c in mock_register.call_args_list}
    assert names == {
        "hikes.scrape_walks",
        "hikes.refresh_forecasts",
        "hikes.prune_windows",
    }


def test_on_startup_jobs_passes_session_as_first_positional():
    """on_startup_jobs() forwards the session as the first positional arg to every call."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    for c in mock_register.call_args_list:
        assert c[0][0] is session


def test_on_startup_jobs_scrape_walks_interval():
    """hikes.scrape_walks is registered with a weekly interval (7 * 86400 s)."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    by_name = {c[1]["name"]: c[1] for c in mock_register.call_args_list}
    assert by_name["hikes.scrape_walks"]["interval_secs"] == 7 * 86400


def test_on_startup_jobs_refresh_forecasts_interval():
    """hikes.refresh_forecasts is registered with a 2-hour interval."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    by_name = {c[1]["name"]: c[1] for c in mock_register.call_args_list}
    assert by_name["hikes.refresh_forecasts"]["interval_secs"] == 2 * 3600


def test_on_startup_jobs_prune_windows_interval():
    """hikes.prune_windows is registered with an hourly interval."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    by_name = {c[1]["name"]: c[1] for c in mock_register.call_args_list}
    assert by_name["hikes.prune_windows"]["interval_secs"] == 3600


def test_on_startup_jobs_all_have_handlers():
    """Every job registered by on_startup_jobs() includes a callable handler."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    for c in mock_register.call_args_list:
        assert callable(c[1]["handler"])


def test_on_startup_jobs_all_have_ttl():
    """Every job registered by on_startup_jobs() includes a positive ttl_secs."""
    import hikes

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        hikes.on_startup_jobs(session)
    for c in mock_register.call_args_list:
        assert c[1]["ttl_secs"] > 0
