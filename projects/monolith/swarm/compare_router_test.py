import json

import pytest

from swarm import compare_router as mod


def _data(**overrides):
    value = {
        "base_sha": "base",
        "commit_sha": "head",
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
