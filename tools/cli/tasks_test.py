"""Tests for the tasks CLI subcommand."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from tools.cli.main import app
from tools.cli.tasks_cmd import _request


@pytest.fixture
def runner():
    return CliRunner()


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_redirect = False
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


class TestListTasks:
    def test_lists_tasks(self, runner):
        """Bare `knowledge tasks` calls GET and formats output."""
        resp = _mock_response(
            {
                "tasks": [
                    {
                        "note_id": "task-1",
                        "title": "Fix the widget",
                        "status": "active",
                        "size": "S",
                        "due": "2026-04-20",
                    },
                    {
                        "note_id": "task-2",
                        "title": "Deploy service",
                        "status": "blocked",
                        "blocked_by": ["task-1"],
                    },
                ]
            }
        )
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with("get", "/api/knowledge/tasks", params={})
        assert "task-1" in result.output
        assert "Fix the widget" in result.output
        assert "[active]" in result.output
        assert "due 2026-04-20" in result.output
        assert "task-2" in result.output
        assert "blocked-by" in result.output

    def test_lists_empty(self, runner):
        """Empty task list prints 'No tasks.'."""
        resp = _mock_response({"tasks": []})
        with patch("tools.cli.tasks_cmd._request", return_value=resp):
            result = runner.invoke(app, ["knowledge", "tasks"])

        assert result.exit_code == 0
        assert "No tasks." in result.output


class TestListTasksFilters:
    def test_status_filter(self, runner):
        """`knowledge tasks --status active` passes status param to _request."""
        resp = _mock_response(
            {
                "tasks": [
                    {
                        "note_id": "task-5",
                        "title": "Active task",
                        "status": "active",
                    }
                ]
            }
        )
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "--status", "active"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "get", "/api/knowledge/tasks", params={"status": "active"}
        )
        assert "task-5" in result.output

    def test_json_output(self, runner):
        """`knowledge tasks --json` prints raw JSON instead of formatted output."""
        data = {
            "tasks": [
                {
                    "note_id": "task-6",
                    "title": "JSON task",
                    "status": "active",
                }
            ]
        }
        resp = _mock_response(data)
        with patch("tools.cli.tasks_cmd._request", return_value=resp):
            result = runner.invoke(app, ["knowledge", "tasks", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed == data
        # Should not contain the formatted task_line output style
        assert "[active]" not in result.output

    def test_status_and_json_combined(self, runner):
        """`knowledge tasks --status blocked --json` passes status param and returns JSON."""
        data = {
            "tasks": [{"note_id": "task-7", "title": "Blocked", "status": "blocked"}]
        }
        resp = _mock_response(data)
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(
                app, ["knowledge", "tasks", "--status", "blocked", "--json"]
            )

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "get", "/api/knowledge/tasks", params={"status": "blocked"}
        )
        parsed = json.loads(result.output)
        assert parsed == data


class TestMarkDone:
    def test_marks_done(self, runner):
        """`knowledge tasks done <id>` calls PATCH with done status."""
        resp = _mock_response({})
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "done", "task-1"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "patch",
            "/api/knowledge/tasks/task-1",
            json={"status": "done"},
        )
        assert "done" in result.output.lower()


class TestCancel:
    def test_cancel(self, runner):
        """`knowledge tasks cancel <id>` calls PATCH with cancelled status."""
        resp = _mock_response({})
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "cancel", "task-1"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "patch",
            "/api/knowledge/tasks/task-1",
            json={"status": "cancelled"},
        )
        assert "cancelled" in result.output.lower()


class TestSearch:
    def test_search(self, runner):
        """`knowledge tasks search "query"` passes q param."""
        resp = _mock_response(
            {
                "tasks": [
                    {
                        "note_id": "task-3",
                        "title": "Search result",
                        "status": "active",
                    }
                ]
            }
        )
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "search", "my query"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "get", "/api/knowledge/tasks", params={"q": "my query"}
        )
        assert "task-3" in result.output
        assert "Search result" in result.output

    def test_search_json_output(self, runner):
        """`knowledge tasks search --json` prints raw JSON for search results."""
        data = {
            "tasks": [
                {
                    "note_id": "task-4",
                    "title": "JSON search result",
                    "status": "active",
                }
            ]
        }
        resp = _mock_response(data)
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(
                app, ["knowledge", "tasks", "search", "--json", "my query"]
            )

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "get", "/api/knowledge/tasks", params={"q": "my query"}
        )
        parsed = json.loads(result.output)
        assert parsed == data
        # Should not contain formatted task_line output
        assert "[active]" not in result.output

    def test_search_empty_results(self, runner):
        """`knowledge tasks search` with no results prints 'No tasks.'."""
        resp = _mock_response({"tasks": []})
        with patch("tools.cli.tasks_cmd._request", return_value=resp):
            result = runner.invoke(app, ["knowledge", "tasks", "search", "nothing"])

        assert result.exit_code == 0
        assert "No tasks." in result.output


class TestBlock:
    def test_block(self, runner):
        """`knowledge tasks block <id>` calls PATCH with blocked status."""
        resp = _mock_response({})
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "block", "task-10"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "patch",
            "/api/knowledge/tasks/task-10",
            json={"status": "blocked"},
        )
        assert "blocked" in result.output.lower()

    def test_block_includes_note_id_in_output(self, runner):
        """`knowledge tasks block <id>` includes the task ID in the output."""
        resp = _mock_response({})
        with patch("tools.cli.tasks_cmd._request", return_value=resp):
            result = runner.invoke(app, ["knowledge", "tasks", "block", "task-99"])

        assert result.exit_code == 0
        assert "task-99" in result.output


class TestActivate:
    def test_activate(self, runner):
        """`knowledge tasks activate <id>` calls PATCH with active status."""
        resp = _mock_response({})
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "activate", "task-20"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with(
            "patch",
            "/api/knowledge/tasks/task-20",
            json={"status": "active"},
        )
        assert "active" in result.output.lower()

    def test_activate_includes_note_id_in_output(self, runner):
        """`knowledge tasks activate <id>` includes the task ID in the output."""
        resp = _mock_response({})
        with patch("tools.cli.tasks_cmd._request", return_value=resp):
            result = runner.invoke(app, ["knowledge", "tasks", "activate", "task-42"])

        assert result.exit_code == 0
        assert "task-42" in result.output


class TestAdd:
    @pytest.mark.parametrize("prefix", [["tasks"], ["knowledge", "tasks"]])
    def test_add_defaults(self, runner, prefix):
        resp = _mock_response({"note_id": "server-generated-id"}, 201)
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as request:
            result = runner.invoke(app, [*prefix, "add", "Buy milk"])

        assert result.exit_code == 0, result.output
        request.assert_called_once_with(
            "post", "/api/knowledge/tasks", json={"title": "Buy milk", "status": "todo"}
        )
        resp.raise_for_status.assert_called_once_with()
        assert result.output == "server-generated-id\n"

    @pytest.mark.parametrize(
        "status", ["todo", "in-progress", "done", "cancelled", "someday"]
    )
    def test_add_options(self, runner, status):
        title = 'Title: "quoted"\nstatus: done'
        resp = _mock_response({"note_id": "another-server-id"}, 201)
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as request:
            result = runner.invoke(
                app, ["tasks", "add", title, "--due", "2028-02-29", "--status", status]
            )

        assert result.exit_code == 0, result.output
        request.assert_called_once_with(
            "post",
            "/api/knowledge/tasks",
            json={"title": title, "due": "2028-02-29", "status": status},
        )
        resp.raise_for_status.assert_called_once_with()
        assert result.output == "another-server-id\n"

    def test_missing_title_never_requests(self, runner):
        with patch("tools.cli.tasks_cmd._request") as request:
            result = runner.invoke(app, ["tasks", "add"])
        assert result.exit_code != 0
        request.assert_not_called()


class TestDaily:
    def test_daily_formatted_output(self, runner):
        """`knowledge tasks daily` calls GET /api/knowledge/tasks/daily and formats output."""
        resp = _mock_response(
            {
                "tasks": [
                    {
                        "note_id": "daily-1",
                        "title": "Morning standup",
                        "status": "active",
                    }
                ]
            }
        )
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "daily"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with("get", "/api/knowledge/tasks/daily")
        assert "daily-1" in result.output
        assert "Morning standup" in result.output
        assert "[active]" in result.output

    def test_daily_empty(self, runner):
        """`knowledge tasks daily` with no tasks prints 'No tasks.'."""
        resp = _mock_response({"tasks": []})
        with patch("tools.cli.tasks_cmd._request", return_value=resp):
            result = runner.invoke(app, ["knowledge", "tasks", "daily"])

        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_daily_json_output(self, runner):
        """`knowledge tasks daily --json` prints raw JSON output."""
        data = {
            "tasks": [
                {
                    "note_id": "daily-2",
                    "title": "Review PRs",
                    "status": "active",
                }
            ]
        }
        resp = _mock_response(data)
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "daily", "--json"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with("get", "/api/knowledge/tasks/daily")
        parsed = json.loads(result.output)
        assert parsed == data
        assert "[active]" not in result.output


class TestWeekly:
    def test_weekly_formatted_output(self, runner):
        """`knowledge tasks weekly` calls GET /api/knowledge/tasks/weekly and formats output."""
        resp = _mock_response(
            {
                "tasks": [
                    {
                        "note_id": "weekly-1",
                        "title": "Weekly planning",
                        "status": "active",
                        "size": "M",
                    }
                ]
            }
        )
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "weekly"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with("get", "/api/knowledge/tasks/weekly")
        assert "weekly-1" in result.output
        assert "Weekly planning" in result.output
        assert "[active]" in result.output

    def test_weekly_empty(self, runner):
        """`knowledge tasks weekly` with no tasks prints 'No tasks.'."""
        resp = _mock_response({"tasks": []})
        with patch("tools.cli.tasks_cmd._request", return_value=resp):
            result = runner.invoke(app, ["knowledge", "tasks", "weekly"])

        assert result.exit_code == 0
        assert "No tasks." in result.output

    def test_weekly_json_output(self, runner):
        """`knowledge tasks weekly --json` prints raw JSON output."""
        data = {
            "tasks": [
                {
                    "note_id": "weekly-2",
                    "title": "Retrospective",
                    "status": "done",
                }
            ]
        }
        resp = _mock_response(data)
        with patch("tools.cli.tasks_cmd._request", return_value=resp) as mock_req:
            result = runner.invoke(app, ["knowledge", "tasks", "weekly", "--json"])

        assert result.exit_code == 0
        mock_req.assert_called_once_with("get", "/api/knowledge/tasks/weekly")
        parsed = json.loads(result.output)
        assert parsed == data
        assert "[done]" not in result.output


class TestRequestReauth:
    def test_reauth_on_redirect(self):
        """_request calls clear_cf_token and retries when first response is_redirect=True."""
        redirect_resp = MagicMock()
        redirect_resp.is_redirect = True

        final_resp = MagicMock()
        final_resp.is_redirect = False
        final_resp.json.return_value = {"tasks": []}
        final_resp.raise_for_status.return_value = None

        # Both calls share the same client mock so we can track call order
        mock_client = MagicMock()
        mock_client.get.side_effect = [redirect_resp, final_resp]

        # _request uses `with client() as http:` twice, so client() must
        # return a fresh context manager on every invocation.
        @contextmanager
        def make_ctx():
            yield mock_client

        with (
            patch("tools.cli.api.client", side_effect=make_ctx),
            patch("tools.cli.api.clear_cf_token") as mock_clear,
        ):
            result = _request("get", "/api/knowledge/tasks/daily")

        mock_clear.assert_called_once()
        assert result is final_resp

    def test_no_reauth_when_not_redirect(self):
        """_request does NOT call clear_cf_token when response is not a redirect."""
        normal_resp = MagicMock()
        normal_resp.is_redirect = False

        mock_client = MagicMock()
        mock_client.get.return_value = normal_resp

        @contextmanager
        def make_ctx():
            yield mock_client

        with (
            patch("tools.cli.api.client", side_effect=make_ctx),
            patch("tools.cli.api.clear_cf_token") as mock_clear,
        ):
            result = _request("get", "/api/knowledge/tasks")

        mock_clear.assert_not_called()
        assert result is normal_resp
