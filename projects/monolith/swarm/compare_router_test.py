import json
import zlib

import httpx
import pytest

from swarm import compare_router as mod


def _data(**overrides):
    value = {
        "base_sha": "base",
        "commit_sha": "head",
        "diff_blob": None,
        "diff_truncated": False,
        "diff_base_sha": None,
        "branch": "claude/swarm-1",
        "repo": "jomcgi/homelab",
        "usage_json": json.dumps(
            {"activities": [{"type": "edit", "file_path": "a.py"}]}
        ),
        "result_text": "RATIONALE\n- path: a.py · why: change it",
    }
    value.update(overrides)
    return value


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_github_get_follows_repository_redirect(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/jomcgi/homelab":
            return httpx.Response(
                301,
                headers={"Location": f"{mod.GITHUB_API}/repositories/12345"},
            )
        return httpx.Response(200, json={"full_name": "jomcgi-org/homelab"})

    real_async_client = httpx.AsyncClient

    def mock_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", mock_async_client)

    response = await mod._github_get(f"{mod.GITHUB_API}/repos/jomcgi/homelab")

    assert response.status_code == 200
    assert response.json()["full_name"] == "jomcgi-org/homelab"
    assert [request.url.path for request in requests] == [
        "/repos/jomcgi/homelab",
        "/repositories/12345",
    ]


def _compare_payload(truncated=False):
    return {
        "files": [
            {
                "filename": "a.py",
                "status": "modified",
                "additions": 2,
                "deletions": 1,
                "changes": 3,
                "patch": "@@ a",
            },
            {
                "filename": "generated.py",
                "status": "added",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "@@ g",
            },
        ],
        "truncated": truncated,
    }


@pytest.mark.asyncio
async def test_sha_resolution_is_stats_first(monkeypatch):
    mod._cache.clear()
    calls = []

    async def get(url):
        calls.append(url)
        return Response(200, _compare_payload())

    monkeypatch.setattr(mod, "_turn_data", lambda *_: _data())
    monkeypatch.setattr(mod, "_github_get", get)
    result = await mod.compare_stats(1, 1)
    assert result["resolution_rung"] == 1
    assert result["diff_type"] == "sha"
    assert result["files"][0]["classification"] == "authored"
    assert result["files"][0]["patch_url"]
    assert result["files"][1]["classification"] == "mechanical"
    assert result["files"][1]["patch_url"] is None
    assert "patch" not in result["files"][0]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stored_diff_is_rung_one_without_github(monkeypatch):
    raw = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "+line\n"
    ).encode()
    monkeypatch.setattr(
        mod,
        "_turn_data",
        lambda *_: _data(
            diff_blob=zlib.compress(raw),
            diff_base_sha="captured-base",
        ),
    )

    async def fail_github(_url):
        raise AssertionError("GitHub must not be called for a stored diff")

    async def fail_compare(*_args, **_kwargs):
        raise AssertionError("GitHub compare must not be called for a stored diff")

    monkeypatch.setattr(mod, "_github_get", fail_github)
    monkeypatch.setattr(mod, "_compare", fail_compare)
    result = await mod.compare_stats(1, 1)

    assert result["resolution_rung"] == 1
    assert result["diff_type"] == "stored"
    assert result["base_sha"] == "captured-base"
    assert result["files"][0]["changes"] == 3


def test_stored_compare_preserves_truncated_status():
    raw = b"diff --git a/plan.json b/plan.json\n"

    result = mod._stored_compare(
        _data(diff_blob=zlib.compress(raw), diff_truncated=True)
    )

    assert result["truncated"] is True
    assert result["source"] == "stored"


@pytest.mark.asyncio
async def test_stored_diff_patch_does_not_call_github(monkeypatch):
    raw = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    ).encode()
    monkeypatch.setattr(
        mod,
        "_turn_data",
        lambda *_: _data(diff_blob=zlib.compress(raw), diff_base_sha="base"),
    )

    async def fail_github(_url):
        raise AssertionError("GitHub must not be called for a stored diff")

    async def fail_compare(*_args, **_kwargs):
        raise AssertionError("GitHub compare must not be called for a stored diff")

    monkeypatch.setattr(mod, "_github_get", fail_github)
    monkeypatch.setattr(mod, "_compare", fail_compare)
    result = await mod.compare_patch(1, 1, "a.py")

    assert result == {"path": "a.py", "patch": "@@ -1 +1 @@\n-old\n+new\n"}


@pytest.mark.asyncio
async def test_branch_and_absence_resolution(monkeypatch):
    mod._cache.clear()

    async def branch_get(url):
        return (
            Response(200, {})
            if "/branches/" in url
            else Response(200, _compare_payload())
        )

    monkeypatch.setattr(
        mod, "_turn_data", lambda *_: _data(base_sha=None, commit_sha=None)
    )
    monkeypatch.setattr(mod, "_github_get", branch_get)
    result = await mod.compare_stats(1, 1)
    assert result["resolution_rung"] == 2
    assert result["diff_type"] == "branch_ephemeral"

    monkeypatch.setattr(
        mod, "_turn_data", lambda *_: _data(base_sha=None, commit_sha=None, branch=None)
    )
    result = await mod.compare_stats(1, 1)
    assert result["resolution_rung"] == 3
    assert result["error"] == "no_compare_available"
    # Issue #4817. This turn's trailer names a.py and no diff was fetched, so
    # the old set difference reported a.py as contradicted: the agent accused
    # of describing work it did not do, on the strength of no evidence at all.
    # The cross-check has to be absent here, not empty and not populated.
    assert result["cross_checked"] is False
    assert "contradicted_paths" not in result
    assert "unexplained_files" not in result


@pytest.mark.asyncio
async def test_base_branch_is_not_treated_as_an_ephemeral_compare(monkeypatch):
    mod._cache.clear()
    calls = []

    async def get(url):
        calls.append(url)
        return Response(200, _compare_payload())

    monkeypatch.setattr(
        mod,
        "_turn_data",
        lambda *_: _data(base_sha=None, commit_sha=None, branch="main"),
    )
    monkeypatch.setattr(mod, "_github_get", get)

    result = await mod.compare_stats(1, 1)

    assert result["resolution_rung"] == 3
    assert result["cross_checked"] is False
    assert result.get("contradicted_paths", []) == []
    assert "contradicted_paths" not in result
    assert calls == []


@pytest.mark.asyncio
async def test_truncation_activities_and_cross_checks(monkeypatch):
    mod._cache.clear()
    activities = [{"type": "edit", "file_path": "a.py"}] + [
        {"type": "read", "file_path": f"x{i}.py"} for i in range(299)
    ]
    data = _data(
        usage_json=json.dumps({"activities": activities}),
        result_text="RATIONALE\n- path: missing.py · why: absent",
    )
    monkeypatch.setattr(mod, "_turn_data", lambda *_: data)
    monkeypatch.setattr(
        mod,
        "_github_get",
        lambda *_: _async_response(Response(200, _compare_payload(True))),
    )
    result = await mod.compare_stats(1, 1)
    assert result["activities_truncated"] is True
    assert result["stats"]["truncated_at"] == 300
    assert result["cross_checked"] is True
    assert result["unexplained_files"] == ["a.py", "generated.py"]
    assert result["contradicted_paths"] == ["missing.py"]


@pytest.mark.asyncio
async def test_lazy_patch_and_cache(monkeypatch):
    mod._cache.clear()
    calls = []

    async def get(url):
        calls.append(url)
        return Response(200, _compare_payload())

    monkeypatch.setattr(mod, "_turn_data", lambda *_: _data())
    monkeypatch.setattr(mod, "_github_get", get)
    await mod.compare_stats(1, 1)
    await mod.compare_stats(1, 1)
    assert len(calls) == 1
    patch = await mod.compare_patch(1, 1, "a.py")
    assert patch == {"path": "a.py", "patch": "@@ a"}


async def _async_response(response):
    return response


@pytest.mark.asyncio
async def test_trailer_named_file_is_authored_without_activities(monkeypatch):
    # Several adapters record no edit/write activities, so the observed set is
    # empty for their turns. Before this, every changed file fell through as
    # mechanical, _mechanical_steps needed run activities it did not have
    # either, and the walkthrough rendered zero steps beside a correct count.
    raw = (
        "diff --git a/swarm/policy.py b/swarm/policy.py\n"
        "--- a/swarm/policy.py\n"
        "+++ b/swarm/policy.py\n"
        "@@ -1 +1,2 @@\n"
        " keep\n"
        "+added\n"
    ).encode()
    monkeypatch.setattr(
        mod,
        "_turn_data",
        lambda *_: _data(
            diff_blob=zlib.compress(raw),
            diff_base_sha="c" * 40,
            result_text=(
                "done\n\nRATIONALE\n- path: swarm/policy.py · why: adds the guard\n"
            ),
            usage_json="{}",
        ),
    )

    async def fail_github(*_a, **_k):
        raise AssertionError("GitHub must not be called for a stored diff")

    monkeypatch.setattr(mod, "_github_get", fail_github)
    monkeypatch.setattr(mod, "_compare", fail_github)
    result = await mod.compare_stats(1, 1)

    assert result["resolution_rung"] == 1
    assert result["files"][0]["classification"] == "authored"
    # The observed set stays reportable and separate from the claim.
    assert result["authored_file_paths"] == []
    assert result["unexplained_files"] == []
    assert result["contradicted_paths"] == []
