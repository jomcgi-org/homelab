"""Tests for the whole-repo interfile FULL scan gather + run (full_scan.py).

Hermetic (no live GitHub, no fc-invoke, no Semgrep App): the GitHub REST calls go
through a fake ``httpx.AsyncClient`` answering the trees/commits/contents
endpoints, and ``scan_files_full`` / ``report_pr_scan`` are mocked.

- ``gather_main_files``: filters the tree to scannable extensions/blobs and
  shapes each result as ``{"path", "content"}``.
- ``run_full_scan``: resolves main's commit sha, times the scan, and calls
  ``report_pr_scan`` with ``is_full_scan=True``, ``branch="main"``, and no
  ``pr_id`` (the whole-repo scan has no PR to attach to).
"""

from __future__ import annotations

import base64
from unittest import mock

import httpx
import pytest

from semgrep_scan import full_scan


def _fake_github_client(*, tree, commit_sha, contents, truncated=False):
    """Stand-in httpx.AsyncClient answering trees/commits/contents GETs."""

    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            if "/git/trees/" in url:
                return _Resp({"tree": tree, "truncated": truncated})
            if "/commits/" in url:
                return _Resp({"sha": commit_sha})
            # contents endpoint: /repos/{repo}/contents/{path}
            path = url.split("/contents/", 1)[1]
            text = contents[path]
            return _Resp(
                {
                    "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                }
            )

    return _Client()


def _tree_entries():
    return [
        {"path": "projects/monolith/semgrep_scan/full_scan.py", "type": "blob"},
        {"path": "projects/monolith/README.md", "type": "blob"},  # unscannable ext
        {"path": "projects/monolith", "type": "tree"},  # a directory, not a blob
        {"path": "projects/firecracker/semgrep/guest-init/main.go", "type": "blob"},
    ]


@pytest.mark.asyncio
async def test_gather_main_files_filters_to_scannable_blobs():
    contents = {
        "projects/monolith/semgrep_scan/full_scan.py": "print('hi')\n",
        "projects/firecracker/semgrep/guest-init/main.go": "package main\n",
    }

    with (
        mock.patch.dict("os.environ", {"GITHUB_API_TOKEN": "test-token"}),
        mock.patch.object(
            full_scan.httpx,
            "AsyncClient",
            return_value=_fake_github_client(
                tree=_tree_entries(), commit_sha="deadbeef", contents=contents
            ),
        ),
    ):
        files = await full_scan.gather_main_files("jomcgi/homelab")

    paths = sorted(f["path"] for f in files)
    assert paths == [
        "projects/firecracker/semgrep/guest-init/main.go",
        "projects/monolith/semgrep_scan/full_scan.py",
    ]
    assert all("content" in f for f in files)
    for f in files:
        assert f["content"] == contents[f["path"]]


def test_excluded_from_baseline():
    """Tests, generated, and minified files are excluded from the baseline scan
    (matching the SMS project path-ignores), source files are kept."""
    excluded = [
        "projects/monolith/semgrep_scan/full_scan_test.py",
        "projects/firecracker/semgrep/guest-init/cmd/main_test.go",
        "projects/monolith/frontend/foo.test.ts",
        "projects/monolith/frontend/vendor.min.js",
        "projects/monolith/grimoire/schema_pb2.py",
        "projects/monolith/semgrep_scan/testdata/real_cli_output.py",
    ]
    kept = [
        "projects/monolith/semgrep_scan/full_scan.py",
        "projects/firecracker/semgrep/guest-init/cmd/main.go",
        "projects/monolith/frontend/src/app.ts",
    ]
    for p in excluded:
        assert full_scan._excluded_from_baseline(p), p
    for p in kept:
        assert not full_scan._excluded_from_baseline(p), p


@pytest.mark.asyncio
async def test_gather_main_files_logs_warning_on_truncated_tree(caplog):
    contents = {"projects/monolith/semgrep_scan/full_scan.py": "x = 1\n"}
    tree = [{"path": "projects/monolith/semgrep_scan/full_scan.py", "type": "blob"}]

    with (
        mock.patch.dict("os.environ", {"GITHUB_API_TOKEN": "test-token"}),
        mock.patch.object(
            full_scan.httpx,
            "AsyncClient",
            return_value=_fake_github_client(
                tree=tree, commit_sha="deadbeef", contents=contents, truncated=True
            ),
        ),
        caplog.at_level("WARNING"),
    ):
        await full_scan.gather_main_files("jomcgi/homelab")

    assert any("truncated" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_run_full_scan_reports_full_scan_on_main():
    scan_result = {"raw_cli_output": {"results": []}}
    report_result = {
        "ok": True,
        "scan_id": 101,
        "findings_reported": 5,
        "project": "jomcgi/homelab-selfhosted",
        "org": "jomcgi",
    }

    with (
        mock.patch.dict("os.environ", {"GITHUB_API_TOKEN": "test-token"}),
        mock.patch.object(
            full_scan,
            "gather_main_files",
            new=mock.AsyncMock(
                return_value=[{"path": "a/b.py", "content": "print(1)\n"}]
            ),
        ),
        mock.patch.object(
            full_scan.httpx,
            "AsyncClient",
            return_value=_fake_github_client(
                tree=[], commit_sha="cafebabe", contents={}
            ),
        ),
        mock.patch.object(
            full_scan, "scan_files_full", new=mock.AsyncMock(return_value=scan_result)
        ) as scan,
        mock.patch.object(
            full_scan,
            "report_pr_scan",
            new=mock.AsyncMock(return_value=report_result),
        ) as report,
    ):
        result = await full_scan.run_full_scan("jomcgi/homelab")

    assert result == report_result
    scan.assert_awaited_once()
    scanned = scan.await_args.args[0]
    assert scanned == [{"path": "a/b.py", "content": "print(1)\n"}]

    report.assert_awaited_once()
    kwargs = report.await_args.kwargs
    assert kwargs["repo"] == "jomcgi/homelab"
    assert kwargs["branch"] == "main"
    assert kwargs["commit"] == "cafebabe"
    assert kwargs["is_full_scan"] is True
    assert kwargs["base_ref"] is None
    assert "pr_id" not in kwargs
    assert kwargs["raw_cli_output"] == {"results": []}
    assert isinstance(kwargs["scan_execution_duration"], float)


@pytest.mark.asyncio
async def test_run_full_scan_returns_error_when_no_files_gathered():
    with (
        mock.patch.dict("os.environ", {"GITHUB_API_TOKEN": "test-token"}),
        mock.patch.object(
            full_scan.httpx,
            "AsyncClient",
            return_value=_fake_github_client(tree=[], commit_sha="cafe", contents={}),
        ),
        mock.patch.object(
            full_scan, "gather_main_files", new=mock.AsyncMock(return_value=[])
        ),
        mock.patch.object(full_scan, "scan_files_full", new=mock.AsyncMock()) as scan,
        mock.patch.object(full_scan, "report_pr_scan", new=mock.AsyncMock()) as report,
    ):
        result = await full_scan.run_full_scan("jomcgi/homelab")

    assert result == {"error": "no files"}
    scan.assert_not_awaited()
    report.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_full_scan_returns_error_when_scan_fails():
    with (
        mock.patch.dict("os.environ", {"GITHUB_API_TOKEN": "test-token"}),
        mock.patch.object(
            full_scan.httpx,
            "AsyncClient",
            return_value=_fake_github_client(tree=[], commit_sha="cafe", contents={}),
        ),
        mock.patch.object(
            full_scan,
            "gather_main_files",
            new=mock.AsyncMock(
                return_value=[{"path": "a/b.py", "content": "print(1)\n"}]
            ),
        ),
        mock.patch.object(
            full_scan,
            "scan_files_full",
            new=mock.AsyncMock(return_value={"error": "fc-invoke down"}),
        ),
        mock.patch.object(full_scan, "report_pr_scan", new=mock.AsyncMock()) as report,
    ):
        result = await full_scan.run_full_scan("jomcgi/homelab")

    assert result == {"error": "fc-invoke down"}
    report.assert_not_awaited()


# Keep an explicit import of httpx so the module reads clearly as exercising the
# fake AsyncClient shape, mirroring router_test.py's style.
assert httpx is not None
