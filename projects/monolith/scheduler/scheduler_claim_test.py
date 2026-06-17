"""Tests for _claim_next_job() in scheduler/api.py.

The research identified that _claim_next_job() issues a PostgreSQL-specific
UPDATE ... WHERE name = (SELECT ... FOR UPDATE SKIP LOCKED) ... RETURNING name
query, but the SQL string itself was never verified in tests — only the
return value was mocked away.

These tests:
- Assert the exact SQL query string passed to session.execute(), including
  the FOR UPDATE SKIP LOCKED clause, correct table/schema references,
  and RETURNING name
- Verify the no-jobs-available path (empty fetchone → returns None)
- Verify the job-found path (fetchone returns a row → returns name string)
- Verify that session.commit() is always called after execute()
- Verify exception propagation when session.execute() raises
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest
from sqlmodel import Session

from scheduler.api import _claim_next_job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session(fetchone_return=None):
    """Return a mock Session whose execute() returns a result with fetchone()."""
    mock_result = MagicMock()
    mock_result.fetchone.return_value = fetchone_return

    mock_session = MagicMock(spec=Session)
    mock_session.execute.return_value = mock_result

    return mock_session, mock_result


# ---------------------------------------------------------------------------
# SQL query content verification
# ---------------------------------------------------------------------------


class TestClaimNextJobSQL:
    def test_execute_is_called_once(self):
        """_claim_next_job() calls session.execute() exactly once."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        assert mock_session.execute.call_count == 1

    def test_sql_contains_for_update_skip_locked(self):
        """The SQL query contains the FOR UPDATE SKIP LOCKED clause."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "FOR UPDATE SKIP LOCKED" in sql_text, (
            f"Expected 'FOR UPDATE SKIP LOCKED' in SQL, got: {sql_text!r}"
        )

    def test_sql_targets_correct_schema_and_table(self):
        """The SQL query references scheduler.scheduled_jobs."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "scheduler.scheduled_jobs" in sql_text, (
            f"Expected 'scheduler.scheduled_jobs' in SQL, got: {sql_text!r}"
        )

    def test_sql_contains_returning_name(self):
        """The SQL query uses RETURNING name to fetch the claimed job name."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "RETURNING name" in sql_text, (
            f"Expected 'RETURNING name' in SQL, got: {sql_text!r}"
        )

    def test_sql_uses_update_with_locked_by_and_locked_at(self):
        """The SQL UPDATE sets locked_by and locked_at columns."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "locked_by" in sql_text, (
            f"Expected 'locked_by' in SQL, got: {sql_text!r}"
        )
        assert "locked_at" in sql_text, (
            f"Expected 'locked_at' in SQL, got: {sql_text!r}"
        )

    def test_sql_filters_by_next_run_at(self):
        """The SQL WHERE clause filters on next_run_at <= :now."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "next_run_at" in sql_text, (
            f"Expected 'next_run_at' in SQL, got: {sql_text!r}"
        )

    def test_sql_passes_hostname_param(self):
        """The SQL is executed with a 'hostname' bind parameter."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        params = mock_session.execute.call_args.args[1]
        assert "hostname" in params, (
            f"Expected 'hostname' in params dict, got: {params!r}"
        )

    def test_sql_passes_now_param(self):
        """The SQL is executed with a 'now' bind parameter."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        params = mock_session.execute.call_args.args[1]
        assert "now" in params, f"Expected 'now' in params dict, got: {params!r}"

    def test_now_param_is_timezone_aware(self):
        """The 'now' bind parameter is a timezone-aware datetime."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        params = mock_session.execute.call_args.args[1]
        now_value = params["now"]
        assert isinstance(now_value, datetime), (
            f"Expected datetime for 'now', got: {type(now_value)}"
        )
        assert now_value.tzinfo is not None, (
            "Expected timezone-aware datetime for 'now', got naive datetime"
        )

    def test_sql_contains_order_by_next_run_at(self):
        """The subquery orders by next_run_at to claim the earliest due job."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "ORDER BY next_run_at" in sql_text, (
            f"Expected 'ORDER BY next_run_at' in SQL, got: {sql_text!r}"
        )

    def test_sql_contains_limit_1(self):
        """The subquery uses LIMIT 1 to claim at most one job per tick."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "LIMIT 1" in sql_text, f"Expected 'LIMIT 1' in SQL, got: {sql_text!r}"

    def test_sql_handles_stale_lock_expiry_via_ttl(self):
        """The SQL releases stale locks using the ttl_secs column."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        sql_arg = mock_session.execute.call_args.args[0]
        sql_text = str(sql_arg)
        assert "ttl_secs" in sql_text, (
            f"Expected 'ttl_secs' in SQL for stale lock expiry, got: {sql_text!r}"
        )


# ---------------------------------------------------------------------------
# Return value behaviour
# ---------------------------------------------------------------------------


class TestClaimNextJobReturnValue:
    def test_returns_none_when_no_jobs_available(self):
        """_claim_next_job returns None when fetchone() returns None (no due jobs)."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        result = _claim_next_job(mock_session)

        assert result is None

    def test_returns_job_name_when_job_claimed(self):
        """_claim_next_job returns the job name string when a row is returned."""
        mock_row = ("my-scheduled-job",)
        mock_session, _ = _make_mock_session(fetchone_return=mock_row)

        result = _claim_next_job(mock_session)

        assert result == "my-scheduled-job"

    def test_returns_first_column_of_row(self):
        """_claim_next_job returns row[0], i.e. the name from RETURNING name."""
        mock_row = ("job-alpha",)
        mock_session, _ = _make_mock_session(fetchone_return=mock_row)

        result = _claim_next_job(mock_session)

        assert result == "job-alpha"

    def test_fetchone_is_called_on_result(self):
        """_claim_next_job calls fetchone() on the execute() result object."""
        mock_session, mock_result = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        mock_result.fetchone.assert_called_once()


# ---------------------------------------------------------------------------
# Session commit behaviour
# ---------------------------------------------------------------------------


class TestClaimNextJobCommit:
    def test_commit_called_when_no_jobs(self):
        """session.commit() is called even when no job is available."""
        mock_session, _ = _make_mock_session(fetchone_return=None)

        _claim_next_job(mock_session)

        mock_session.commit.assert_called_once()

    def test_commit_called_when_job_claimed(self):
        """session.commit() is called after successfully claiming a job."""
        mock_row = ("commit-test-job",)
        mock_session, _ = _make_mock_session(fetchone_return=mock_row)

        _claim_next_job(mock_session)

        mock_session.commit.assert_called_once()

    def test_commit_called_after_execute(self):
        """session.commit() is called after session.execute(), not before."""
        call_order = []

        mock_result = MagicMock()
        mock_result.fetchone.return_value = None

        mock_session = MagicMock(spec=Session)
        mock_session.execute.side_effect = lambda *a, **kw: (
            call_order.append("execute") or mock_result
        )
        mock_session.commit.side_effect = lambda: call_order.append("commit")

        _claim_next_job(mock_session)

        assert call_order == ["execute", "commit"], (
            f"Expected execute then commit, got: {call_order}"
        )


# ---------------------------------------------------------------------------
# Exception propagation
# ---------------------------------------------------------------------------


class TestClaimNextJobExceptions:
    def test_database_error_propagates(self):
        """Exceptions raised by session.execute() are not swallowed."""
        mock_session = MagicMock(spec=Session)
        mock_session.execute.side_effect = RuntimeError("DB connection lost")

        with pytest.raises(RuntimeError, match="DB connection lost"):
            _claim_next_job(mock_session)

    def test_commit_not_called_on_execute_error(self):
        """session.commit() is not called when session.execute() raises."""
        mock_session = MagicMock(spec=Session)
        mock_session.execute.side_effect = Exception("execute failed")

        with pytest.raises(Exception, match="execute failed"):
            _claim_next_job(mock_session)

        mock_session.commit.assert_not_called()

    def test_fetchone_error_propagates(self):
        """Exceptions raised by fetchone() are not swallowed."""
        mock_result = MagicMock()
        mock_result.fetchone.side_effect = RuntimeError("cursor closed")

        mock_session = MagicMock(spec=Session)
        mock_session.execute.return_value = mock_result

        with pytest.raises(RuntimeError, match="cursor closed"):
            _claim_next_job(mock_session)
