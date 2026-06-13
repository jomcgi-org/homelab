"""Tests for stars.__init__: router registration and startup job wiring."""

from unittest.mock import MagicMock, patch


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


def test_on_startup_jobs_calls_register_job_four_times():
    """on_startup_jobs() calls register_job exactly four times."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    assert mock_register.call_count == 4


def test_on_startup_jobs_correct_names():
    """on_startup_jobs() registers the four stars scheduled jobs."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    names = {c[1]["name"] for c in mock_register.call_args_list}
    assert names == {
        "stars.load_grid",
        "stars.load_climatology",
        "stars.refresh",
        "stars.prune_hours",
    }


def test_on_startup_jobs_load_climatology_interval():
    """stars.load_climatology is registered with a 24-hour interval."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    by_name = {c[1]["name"]: c[1] for c in mock_register.call_args_list}
    assert by_name["stars.load_climatology"]["interval_secs"] == 24 * 3600


def test_on_startup_jobs_passes_session_as_first_positional():
    """on_startup_jobs() forwards the session as the first positional arg to every call."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    for c in mock_register.call_args_list:
        assert c[0][0] is session


def test_on_startup_jobs_load_grid_interval():
    """stars.load_grid is registered with a 6-hour interval."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    by_name = {c[1]["name"]: c[1] for c in mock_register.call_args_list}
    assert by_name["stars.load_grid"]["interval_secs"] == 6 * 3600


def test_on_startup_jobs_refresh_interval():
    """stars.refresh is registered with a 3-hour interval."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    by_name = {c[1]["name"]: c[1] for c in mock_register.call_args_list}
    assert by_name["stars.refresh"]["interval_secs"] == 3 * 3600


def test_on_startup_jobs_prune_hours_interval():
    """stars.prune_hours is registered with an hourly interval."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    by_name = {c[1]["name"]: c[1] for c in mock_register.call_args_list}
    assert by_name["stars.prune_hours"]["interval_secs"] == 3600


def test_on_startup_jobs_all_have_handlers():
    """Every job registered by on_startup_jobs() includes a callable handler."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    for c in mock_register.call_args_list:
        assert callable(c[1]["handler"])


def test_on_startup_jobs_all_have_ttl():
    """Every job registered by on_startup_jobs() includes a positive ttl_secs."""
    import stars

    session = MagicMock()
    with patch("shared.scheduler.register_job") as mock_register:
        stars.on_startup_jobs(session)
    for c in mock_register.call_args_list:
        assert c[1]["ttl_secs"] > 0
