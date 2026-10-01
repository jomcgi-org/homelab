"""Network-free tests for authoritative repository comparisons."""

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
import re

import httpx
import pytest

from knowledge.repo_diff_source import (
    REPO_DIFF_EXCLUSIONS,
    REPO_DIFF_PATCH_CAP,
    RepoDiffRangeInvalid,
    RepoDiffSourceUnavailable,
    collect_repo_diff,
    verify_on_main,
)

BASE = "a" * 40
HEAD = "b" * 40
MAIN = "c" * 40


def _file(filename="src/example.py", **overrides):
    return {
        "filename": filename,
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "patch": "@@ -1 +1 @@\n-old\n+new",
        **overrides,
    }


def _body(**overrides):
    return {
        "status": "ahead",
        "base_commit": {"sha": BASE},
        "merge_base_commit": {"sha": BASE},
        "commits": [{"sha": HEAD}],
        "total_commits": 1,
        "files": [_file()],
        **overrides,
    }


def _main(**overrides):
    return _body(
        **{
            "base_commit": {"sha": HEAD},
            "merge_base_commit": {"sha": HEAD},
            "commits": [{"sha": MAIN}],
            **overrides,
        }
    )


def _client(body=None, *, main=None, status_code=200, response=None):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("...main"):
            sha = request.url.path.rsplit("/", 1)[-1].split("...")[0]
            default_main = _main(
                base_commit={"sha": sha}, merge_base_commit={"sha": sha}
            )
            return httpx.Response(200, json=default_main if main is None else main)
        if response is not None:
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(status_code, json=_body() if body is None else body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def test_ahead_evidence_and_coverage():
    client, requests = _client()
    with client:
        evidence = collect_repo_diff(BASE, HEAD, client=client, repo="owner/repo")
    assert [r.url.path for r in requests] == [
        f"/repos/owner/repo/compare/{BASE}...{HEAD}",
        f"/repos/owner/repo/compare/{HEAD}...main",
    ]
    assert evidence.base_sha == BASE
    assert evidence.head_sha == HEAD
    assert evidence.compare_status == "ahead"
    assert evidence.evidence_source == "github-compare"
    assert evidence.total_commits == 1
    assert evidence.changed_files == 1
    assert evidence.additions == evidence.deletions == 1
    assert evidence.diff_stat == "src/example.py | 2 +1 -1"
    assert evidence.patch == (
        "diff --git a/src/example.py b/src/example.py\n"
        "--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    assert evidence.coverage == {
        "files_listed": 1,
        "files_excluded": 0,
        "files_included": 1,
        "files_patch_included": 1,
        "files_patch_omitted_by_github": 0,
        "files_patch_cut_by_cap": 0,
        "patch_chars": len(evidence.patch),
        "patch_truncated": False,
        "file_list_complete": True,
        "total_commits": 1,
    }
    with pytest.raises(FrozenInstanceError):
        evidence.head_sha = MAIN


def test_identical_without_files():
    body = _body(status="identical", total_commits=0, commits=[])
    del body["files"]
    client, requests = _client(body)
    with client:
        evidence = collect_repo_diff(BASE, BASE, client=client)
    assert evidence.patch == evidence.diff_stat == ""
    assert evidence.changed_files == evidence.total_commits == 0
    assert evidence.additions == evidence.deletions == 0
    assert evidence.coverage["file_list_complete"] is True
    assert len(requests) == 2


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_nonforward_range(status):
    client, requests = _client(_body(status=status))
    with client, pytest.raises(RepoDiffRangeInvalid):
        collect_repo_diff(BASE, HEAD, client=client)
    assert len(requests) == 1


@pytest.mark.parametrize("status_code", [404, 422])
def test_range_http_errors(status_code):
    client, requests = _client(status_code=status_code)
    with client, pytest.raises(RepoDiffRangeInvalid):
        collect_repo_diff(BASE, HEAD, client=client)
    assert len(requests) == 1


@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 502, 503])
def test_unavailable_http_errors_without_retry(status_code):
    client, requests = _client(status_code=status_code)
    with client, pytest.raises(RepoDiffSourceUnavailable):
        collect_repo_diff(BASE, HEAD, client=client)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("transport failed"),
        httpx.Response(200, content="not JSON"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=None),
        httpx.Response(200, json="object missing"),
    ],
)
def test_unavailable_source(response):
    client, requests = _client(response=response)
    with client, pytest.raises(RepoDiffSourceUnavailable):
        collect_repo_diff(BASE, HEAD, client=client)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_commit": {"sha": MAIN}},
        {"merge_base_commit": {"sha": MAIN}},
        {"commits": [{"sha": MAIN}]},
        {"status": "identical", "total_commits": 0},
        {"total_commits": 0},
    ],
)
def test_range_mismatch(overrides):
    client, _ = _client(_body(**overrides))
    with client, pytest.raises(RepoDiffRangeInvalid):
        collect_repo_diff(BASE, HEAD, client=client)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", None),
        ("status", []),
        ("status", "unknown"),
        ("base_commit", []),
        ("base_commit", {"sha": None}),
        ("merge_base_commit", None),
        ("merge_base_commit", []),
        ("commits", None),
        ("commits", {}),
        ("commits", []),
        ("commits", [None]),
        ("commits", [{"sha": None}]),
        ("files", None),
        ("files", {}),
        ("total_commits", None),
        ("total_commits", True),
        ("total_commits", -1),
        ("total_commits", "1"),
    ],
)
def test_malformed_fields_fail_closed(field, value):
    client, _ = _client(_body(**{field: value}))
    with client, pytest.raises(RepoDiffSourceUnavailable):
        collect_repo_diff(BASE, HEAD, client=client)


@pytest.mark.parametrize("field", ["files", "total_commits", "commits"])
def test_missing_fields_fail_closed(field):
    body = _body()
    del body[field]
    client, _ = _client(body)
    with client, pytest.raises(RepoDiffSourceUnavailable):
        collect_repo_diff(BASE, HEAD, client=client)


def test_merge_base_fallback():
    body = _body()
    del body["base_commit"]
    client, _ = _client(body)
    with client:
        assert collect_repo_diff(BASE, HEAD, client=client).base_sha == BASE


@pytest.mark.parametrize("status", ["behind", "diverged", "unknown", None, []])
def test_head_not_reachable_on_main(status):
    client, requests = _client(main=_main(status=status))
    with client, pytest.raises(RepoDiffRangeInvalid):
        collect_repo_diff(BASE, HEAD, client=client)
    assert len(requests) == 2


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_verify_on_main_for_first_run(status):
    client, requests = _client(main=_main(status=status))
    with client:
        assert verify_on_main(HEAD.upper(), client=client) is None
    assert len(requests) == 1
    assert requests[0].url.path.endswith(f"/{HEAD}...main")


def test_main_verification_also_checks_base():
    client, _ = _client(main=_main(base_commit={"sha": BASE}))
    with client, pytest.raises(RepoDiffRangeInvalid):
        collect_repo_diff(BASE, HEAD, client=client)


def test_uppercase_shas_normalized():
    client, requests = _client(
        _body(base_commit={"sha": BASE.upper()}, commits=[{"sha": HEAD.upper()}])
    )
    with client:
        evidence = collect_repo_diff(BASE.upper(), HEAD.upper(), client=client)
    assert evidence.base_sha == BASE
    assert evidence.head_sha == HEAD
    assert requests[0].url.path.endswith(f"/{BASE}...{HEAD}")


@pytest.mark.parametrize(
    "sha", [None, 123, True, [], {}, "", "a" * 39, "a" * 41, "g" * 40, BASE + "\n"]
)
@pytest.mark.parametrize("position", ["base", "head", "main"])
def test_invalid_sha_before_requests(sha, position):
    client, requests = _client()
    with client, pytest.raises(RepoDiffRangeInvalid):
        if position == "main":
            verify_on_main(sha, client=client)
        else:
            collect_repo_diff(
                sha if position == "base" else BASE,
                sha if position == "head" else HEAD,
                client=client,
            )
    assert requests == []


@pytest.mark.parametrize(
    "timeout", [None, True, "15", 0, -1, float("nan"), float("inf")]
)
def test_invalid_timeout_before_requests(timeout):
    client, requests = _client()
    with client, pytest.raises(ValueError):
        collect_repo_diff(BASE, HEAD, client=client, timeout=timeout)
    assert requests == []


def test_timeout_is_bounded():
    client, requests = _client()
    with client:
        collect_repo_diff(BASE, HEAD, client=client, timeout=60)
    assert all(max(r.extensions["timeout"].values()) <= 15 for r in requests)


def test_redirects_followed_on_injected_client():
    requests = []

    def handler(request):
        requests.append(request)
        if "/repos/old/repo/" in request.url.path:
            return httpx.Response(
                301,
                headers={"Location": str(request.url).replace("old/repo", "new/repo")},
            )
        return httpx.Response(
            200, json=_main() if request.url.path.endswith("...main") else _body()
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert (
            collect_repo_diff(BASE, HEAD, repo="old/repo", client=client).changed_files
            == 1
        )
    assert len(requests) == 4


@pytest.mark.parametrize(
    "status,previous,before,after",
    [
        ("renamed", "old.py", "a/old.py", "b/new.py"),
        ("added", None, "/dev/null", "b/new.py"),
        ("removed", None, "a/new.py", "/dev/null"),
        ("modified", None, "a/new.py", "b/new.py"),
        ("modified", 4, "a/new.py", "b/new.py"),
    ],
)
def test_file_headers(status, previous, before, after):
    client, _ = _client(
        _body(files=[_file("new.py", status=status, previous_filename=previous)])
    )
    with client:
        patch = collect_repo_diff(BASE, HEAD, client=client).patch
    assert f"--- {before}\n+++ {after}\n" in patch
    old = previous if isinstance(previous, str) else "new.py"
    assert patch.startswith(f"diff --git a/{old} b/new.py\n")


def test_quoted_paths_and_empty_previous_filename():
    client, _ = _client(_body(files=[_file("new name.py", previous_filename="")]))
    with client:
        patch = collect_repo_diff(BASE, HEAD, client=client).patch
    assert patch.startswith('diff --git "a/new name.py" "b/new name.py"\n')


@pytest.mark.parametrize("patch", [None, "", 123, {}])
def test_omitted_patch_counted(patch):
    client, _ = _client(_body(files=[_file(patch=patch)]))
    with client:
        evidence = collect_repo_diff(BASE, HEAD, client=client)
    assert evidence.patch == ""
    assert evidence.changed_files == 1
    assert evidence.coverage["files_patch_omitted_by_github"] == 1
    assert evidence.coverage["files_patch_cut_by_cap"] == 0


def test_absent_patch_and_malformed_file_items():
    binary = _file()
    del binary["patch"]
    client, _ = _client(
        _body(
            files=[
                None,
                [],
                3,
                {},
                {"filename": None},
                {"filename": 4},
                {"filename": ""},
                binary,
            ]
        )
    )
    with client:
        evidence = collect_repo_diff(BASE, HEAD, client=client)
    assert evidence.changed_files == 1
    assert evidence.coverage["files_listed"] == 8
    assert evidence.coverage["files_patch_omitted_by_github"] == 1


@pytest.mark.parametrize("field", ["additions", "deletions", "changes"])
@pytest.mark.parametrize("value", [None, True, -1, "1", 1.0])
def test_malformed_file_counts(field, value):
    client, _ = _client(_body(files=[_file(**{field: value})]))
    with client, pytest.raises(RepoDiffSourceUnavailable):
        collect_repo_diff(BASE, HEAD, client=client)


def test_all_prompt_exclusions_are_declared():
    tree = ast.parse(Path(__file__).with_name("extraction.py").read_text())
    prompt = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_repo_diff_prompt"
    )
    literals = " ".join(
        node.value
        for node in ast.walk(prompt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    exclusions = literals.split("Exclude", 1)[1].split("Use git pathspec", 1)[0]
    assert set(re.findall(r"`([^`]+)`", exclusions)) == set(REPO_DIFF_EXCLUSIONS)


def test_excluded_only_comparison():
    paths = [
        "nested/a.lock",
        "nested/go.sum",
        "BUILD",
        "src/BUILD.bazel",
        "src/foo_manifest.ndjson",
        "src/foo-manifest.json",
        "pnpm-lock.yaml",
        "requirements.txt",
        "src/requirements-dev.txt",
        "db/atlas.sum",
        "bazel-out/src/example.py",
        "bazel-bin/example.py",
    ]
    client, _ = _client(_body(files=[_file(path) for path in paths]))
    with client:
        evidence = collect_repo_diff(BASE, HEAD, client=client)
    assert evidence.patch == evidence.diff_stat == ""
    assert evidence.changed_files == evidence.additions == evidence.deletions == 0
    assert evidence.coverage["files_excluded"] == len(paths)
    assert evidence.coverage["files_included"] == 0


def test_nested_bazel_directory_is_not_top_level_exclusion():
    client, _ = _client(_body(files=[_file("src/bazel-custom/source.py")]))
    with client:
        assert collect_repo_diff(BASE, HEAD, client=client).changed_files == 1


def test_large_comparison_capped_with_whole_files():
    files = [
        _file("first.py"),
        _file("huge.py", patch="+" + "x" * REPO_DIFF_PATCH_CAP),
        _file("last.py"),
        _file("binary", patch=None),
    ]
    client, _ = _client(_body(files=files))
    with client:
        evidence = collect_repo_diff(BASE, HEAD, client=client)
    assert len(evidence.patch) <= REPO_DIFF_PATCH_CAP
    assert evidence.patch.splitlines()[-1] == "[... elided ...]"
    assert "diff --git a/first.py" in evidence.patch
    assert "huge.py" not in evidence.patch
    assert "last.py" not in evidence.patch
    assert evidence.coverage["files_listed"] == 4
    assert evidence.coverage["files_included"] == 4
    assert evidence.coverage["files_patch_included"] == 1
    assert evidence.coverage["files_patch_omitted_by_github"] == 1
    assert evidence.coverage["files_patch_cut_by_cap"] == 2
    assert evidence.coverage["patch_chars"] == len(evidence.patch)
    assert evidence.coverage["patch_truncated"] is True


def test_cap_boundary_reserves_marker_by_removing_whole_file():
    client, _ = _client(_body(files=[_file("first.py", patch="x")]))
    with client:
        overhead = len(collect_repo_diff(BASE, HEAD, client=client).patch) - 1
    files = [
        _file("first.py", patch="x" * (REPO_DIFF_PATCH_CAP - overhead)),
        _file("last.py"),
    ]
    client, _ = _client(_body(files=files))
    with client:
        evidence = collect_repo_diff(BASE, HEAD, client=client)
    assert evidence.patch == "[... elided ...]\n"
    assert evidence.coverage["files_patch_cut_by_cap"] == 2
    assert evidence.coverage["files_patch_included"] == 0


@pytest.mark.parametrize("size,complete", [(299, True), (300, False), (301, False)])
def test_file_list_cap_not_paged(size, complete):
    client, requests = _client(
        _body(files=[_file(f"{i}.py", patch=None) for i in range(size)])
    )
    with client:
        evidence = collect_repo_diff(BASE, HEAD, client=client)
    assert evidence.coverage["file_list_complete"] is complete
    assert evidence.changed_files == size
    assert evidence.coverage["files_listed"] == size
    assert len(requests) == 2


@pytest.mark.parametrize("token", [None, "", "api-token"])
def test_authorization_uses_only_api_token(monkeypatch, token):
    monkeypatch.setenv("GITHUB_TOKEN", "kloak-placeholder")
    if token is None:
        monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_API_TOKEN", token)
    client, requests = _client()
    with client:
        collect_repo_diff(BASE, HEAD, client=client)
    assert len(requests) == 2
    for request in requests:
        if token:
            assert request.headers["Authorization"] == f"Bearer {token}"
        else:
            assert "Authorization" not in request.headers


def test_owned_client_lifecycle(monkeypatch):
    client, requests = _client()
    monkeypatch.setattr(httpx, "Client", lambda: client)
    assert collect_repo_diff(BASE, HEAD).changed_files == 1
    assert client.is_closed
    assert len(requests) == 2


def test_owned_main_client_lifecycle_on_error(monkeypatch):
    client, _ = _client(main=_main(status="behind"))
    monkeypatch.setattr(httpx, "Client", lambda: client)
    with pytest.raises(RepoDiffRangeInvalid):
        verify_on_main(HEAD)
    assert client.is_closed
