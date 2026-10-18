"""Unit tests for scheduler/views.py — SchedulerJobView wire shape."""

from datetime import datetime, timezone

from scheduler.views import SchedulerJobView


def test_excludes_lock_columns():
    """Lock metadata is intentionally absent from the wire shape."""
    view = SchedulerJobView(
        name="job",
        interval_secs=60,
        ttl_secs=300,
        next_run_at=datetime.now(timezone.utc),
        last_run_at=None,
        last_status=None,
    )
    dumped = view.model_dump()
    assert "locked_by" not in dumped
    assert "locked_at" not in dumped
    assert "has_handler" not in dumped


def test_optional_fields_default_to_none():
    """last_run_at and last_status default to None when omitted."""
    view = SchedulerJobView(
        name="job",
        interval_secs=60,
        ttl_secs=300,
        next_run_at=datetime.now(timezone.utc),
    )
    assert view.last_run_at is None
    assert view.last_status is None


def test_round_trip_through_dict():
    """A view can be serialized and round-tripped through dict."""
    now = datetime(2026, 4, 25, 14, 0, 0, tzinfo=timezone.utc)
    original = SchedulerJobView(
        name="knowledge.gardener",
        interval_secs=600,
        ttl_secs=300,
        next_run_at=now,
        last_run_at=now,
        last_status="ok",
    )
    rebuilt = SchedulerJobView.model_validate(original.model_dump())
    assert rebuilt == original
