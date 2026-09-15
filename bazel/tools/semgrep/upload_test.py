"""Hermetic tests for the best-effort Semgrep App lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bazel.tools.semgrep.upload import TIMEOUT, upload


def _response(*, scan_id="scan-42", error=None):
    response = MagicMock()
    response.json.return_value = {"info": {"id": scan_id}}
    if error is not None:
        response.raise_for_status.side_effect = error
    return response


def _client(stage_failure=None):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    responses = {
        "registration": _response(),
        "findings upload": _response(),
        "completion": _response(),
    }
    if stage_failure:
        responses[stage_failure] = _response(error=RuntimeError(stage_failure))
    client.post.side_effect = list(responses.values())
    return client


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SEMGREP_APP_TOKEN", "test-token")
    monkeypatch.setenv("SEMGREP_URL", "https://semgrep.example")
    monkeypatch.setenv("SEMGREP_REPO", "org/repo")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_REF_NAME", "feature/scan")
    monkeypatch.setenv("SEMGREP_ENGINE_VERSION", "1.168.0")


@pytest.mark.parametrize(
    ("results", "scan_exit"),
    [
        ({"results": [], "errors": []}, 0),
        ({"results": [{"check_id": "fixture.finding"}], "errors": []}, 1),
    ],
    ids=["clean", "findings"],
)
def test_successful_lifecycle_request_sequence_and_payloads(
    tmp_path, configured, results, scan_exit, capsys
):
    path = tmp_path / "results.json"
    path.write_text(json.dumps(results))
    client = _client()

    with patch(
        "bazel.tools.semgrep.upload.httpx.Client", return_value=client
    ) as factory:
        upload(path, scan_exit)

    factory.assert_called_once_with(timeout=TIMEOUT)
    assert [request.args[0] for request in client.post.call_args_list] == [
        "https://semgrep.example/api/cli/scans",
        "https://semgrep.example/api/agent/scans/scan-42/results",
        "https://semgrep.example/api/agent/scans/scan-42/complete",
    ]
    register = client.post.call_args_list[0].kwargs
    assert register["headers"]["Authorization"] == "Bearer test-token"
    assert register["json"]["scan_metadata"] | {"unique_id": "ignored"} == {
        "cli_version": "1.168.0",
        "unique_id": "ignored",
        "requested_products": ["sast"],
        "dry_run": False,
    }
    assert register["json"]["project_metadata"] == {
        "semgrep_version": "1.168.0",
        "repository": "org/repo",
        "repo_url": "https://github.com/org/repo",
        "branch": "feature/scan",
        "commit": "a" * 40,
        "is_full_scan": True,
    }
    assert client.post.call_args_list[1].kwargs["json"] == results
    assert client.post.call_args_list[2].kwargs["json"] == {"exit_code": scan_exit}
    assert "completed Semgrep App scan scan-42" in capsys.readouterr().err


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_or_empty_token_never_starts_remote_lifecycle(
    tmp_path, monkeypatch, token, capsys
):
    if token is None:
        monkeypatch.delenv("SEMGREP_APP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("SEMGREP_APP_TOKEN", token)
    path = tmp_path / "results.json"
    path.write_text('{"results": [], "errors": []}')

    with patch("bazel.tools.semgrep.upload.httpx.Client") as factory:
        upload(path, 0)

    factory.assert_not_called()
    assert "SEMGREP_APP_TOKEN is empty" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("stage", "expected_requests"),
    [("registration", 1), ("findings upload", 3), ("completion", 3)],
)
def test_each_remote_stage_failure_is_nonfatal_and_preserves_sequence(
    tmp_path, configured, stage, expected_requests, capsys
):
    path = tmp_path / "results.json"
    path.write_text('{"results": [{"check_id": "fixture.finding"}], "errors": []}')
    client = _client(stage)

    with patch("bazel.tools.semgrep.upload.httpx.Client", return_value=client):
        upload(path, 1)

    assert client.post.call_count == expected_requests
    diagnostic = capsys.readouterr().err
    assert f"{stage} failed (non-fatal)" in diagnostic
    if stage == "findings upload":
        assert client.post.call_args_list[-1].args[0].endswith("/complete")
        assert client.post.call_args_list[-1].kwargs["json"] == {"exit_code": 1}


def test_client_setup_failure_is_nonfatal(tmp_path, configured, capsys):
    path = tmp_path / "results.json"
    path.write_text('{"results": [], "errors": []}')

    with patch(
        "bazel.tools.semgrep.upload.httpx.Client",
        side_effect=RuntimeError("offline"),
    ):
        upload(path, 0)

    assert "client setup failed (non-fatal)" in capsys.readouterr().err


@pytest.mark.parametrize("contents", ["not json", "[]"])
def test_unreadable_result_never_starts_remote_lifecycle(
    tmp_path, configured, contents, capsys
):
    path = tmp_path / "results.json"
    path.write_text(contents)

    with patch("bazel.tools.semgrep.upload.httpx.Client") as factory:
        upload(path, 0)

    if contents == "not json":
        factory.assert_not_called()
        assert "reading results failed (non-fatal)" in capsys.readouterr().err
    else:
        # Shape validation belongs to the scan wrapper. The upload helper sends
        # an already-validated JSON document without silently rewriting it.
        factory.assert_called_once()


def test_metadata_fallback_uses_environment_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMGREP_APP_TOKEN", "token")
    monkeypatch.delenv("SEMGREP_REPO", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "fallback/repo")
    path = tmp_path / "results.json"
    path.write_text('{"results": [], "errors": []}')
    client = _client()

    with patch("bazel.tools.semgrep.upload.httpx.Client", return_value=client):
        upload(Path(path), 0)

    metadata = client.post.call_args_list[0].kwargs["json"]["project_metadata"]
    assert metadata["repository"] == "fallback/repo"


def test_remote_errors_cannot_leak_the_app_token(tmp_path, configured, capsys):
    path = tmp_path / "results.json"
    path.write_text('{"results": [], "errors": []}')
    client = _client()
    client.post.side_effect = RuntimeError("request rejected test-token")

    with patch("bazel.tools.semgrep.upload.httpx.Client", return_value=client):
        upload(path, 0)

    diagnostic = capsys.readouterr().err
    assert "test-token" not in diagnostic
    assert "[REDACTED]" in diagnostic
