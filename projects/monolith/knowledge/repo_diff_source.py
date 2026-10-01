"""Bounded repository evidence collected from GitHub, without cursor writes."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
import math
from pathlib import PurePosixPath
import re

import httpx

from core.github import GITHUB_API, GITHUB_REPO, _github_headers

REPO_DIFF_PATCH_CAP = 60_000
REPO_DIFF_EXCLUSIONS = (
    "*.lock",
    "*.sum",
    "BUILD",
    "BUILD.bazel",
    "*_manifest.ndjson",
    "*-manifest.json",
    "pnpm-lock.yaml",
    "requirements*.txt",
    "atlas.sum",
    "bazel-*",
)
_ELISION = "[... elided ...]"


class RepoDiffSourceUnavailable(RuntimeError):
    """Source evidence could not be obtained; the caller can try next interval."""


class RepoDiffRangeInvalid(ValueError):
    """The requested range does not identify a forward comparison on main."""


@dataclass(frozen=True)
class RepoDiffEvidence:
    base_sha: str
    head_sha: str
    compare_status: str
    total_commits: int
    diff_stat: str
    patch: str
    changed_files: int
    additions: int
    deletions: int
    coverage: dict[str, int | bool]
    evidence_source: str = "github-compare"


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
        raise RepoDiffRangeInvalid("A full 40-hex commit SHA is required")
    return value.lower()


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a finite positive number")
    return min(value, 15.0)


def _compare(
    client: httpx.Client, repo: str, base: str, head: str, timeout: float
) -> dict:
    try:
        response = client.get(
            f"{GITHUB_API}/repos/{repo}/compare/{base}...{head}",
            headers=_github_headers(),
            follow_redirects=True,
            timeout=timeout,
        )
        if response.status_code in (404, 422):
            raise RepoDiffRangeInvalid(
                f"GitHub rejected comparison ({response.status_code})"
            )
        if response.status_code != 200:
            raise RepoDiffSourceUnavailable(
                f"GitHub comparison unavailable ({response.status_code})"
            )
        body = response.json()
    except httpx.RequestError as exc:
        raise RepoDiffSourceUnavailable("GitHub comparison request failed") from exc
    except ValueError as exc:
        if isinstance(exc, RepoDiffRangeInvalid):
            raise
        raise RepoDiffSourceUnavailable("GitHub comparison is not JSON") from exc
    if not isinstance(body, dict):
        raise RepoDiffSourceUnavailable("GitHub comparison is not an object")
    return body


def _commit_sha(value: object) -> str:
    if not isinstance(value, dict):
        raise RepoDiffSourceUnavailable("GitHub comparison is missing a commit object")
    try:
        return _sha(value.get("sha"))
    except RepoDiffRangeInvalid as exc:
        raise RepoDiffSourceUnavailable(
            "GitHub comparison is missing a full commit SHA"
        ) from exc


def _verify_base(body: dict, base: str) -> None:
    base_commit = body.get("base_commit")
    if base_commit is None:
        base_commit = body.get("merge_base_commit")
    if _commit_sha(base_commit) != base:
        raise RepoDiffRangeInvalid(
            "GitHub comparison base does not match the requested SHA"
        )
    if (
        body.get("status") == "ahead"
        and _commit_sha(body.get("merge_base_commit")) != base
    ):
        raise RepoDiffRangeInvalid(
            "GitHub comparison does not descend from the requested base"
        )


def verify_on_main(
    sha: str,
    *,
    repo: str = GITHUB_REPO,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> None:
    """Require main to equal or descend from sha, including first-run cursors."""
    sha = _sha(sha)
    timeout = _timeout(timeout)
    if client is None:
        with httpx.Client() as owned_client:
            return verify_on_main(sha, repo=repo, client=owned_client, timeout=timeout)
    body = _compare(client, repo, sha, "main", timeout)
    if body.get("status") not in ("ahead", "identical"):
        raise RepoDiffRangeInvalid("Requested head is not reachable from main")
    _verify_base(body, sha)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepoDiffSourceUnavailable("GitHub comparison has an invalid count")
    return value


def _excluded(filename: str) -> bool:
    path = PurePosixPath(filename)
    return (len(path.parts) > 1 and fnmatchcase(path.parts[0], "bazel-*")) or any(
        fnmatchcase(path.name, pattern) for pattern in REPO_DIFF_EXCLUSIONS[:-1]
    )


def _git_path(prefix: str, filename: str) -> str:
    path = f"{prefix}/{filename}"
    if any(char.isspace() or char in '\\"' for char in path):
        return json.dumps(path, ensure_ascii=False)
    return path


def collect_repo_diff(
    base_sha: str,
    head_sha: str,
    *,
    repo: str = GITHUB_REPO,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> RepoDiffEvidence:
    """Collect one exact comparison and main ancestry check, without retries."""
    base_sha, head_sha = _sha(base_sha), _sha(head_sha)
    timeout = _timeout(timeout)
    if client is None:
        with httpx.Client() as owned_client:
            return collect_repo_diff(
                base_sha, head_sha, repo=repo, client=owned_client, timeout=timeout
            )
    body = _compare(client, repo, base_sha, head_sha, timeout)
    status = body.get("status")
    if status in ("behind", "diverged"):
        raise RepoDiffRangeInvalid("GitHub comparison is not forward")
    if status not in ("ahead", "identical"):
        raise RepoDiffSourceUnavailable("GitHub comparison has no supported status")
    _verify_base(body, base_sha)
    total_commits = _count(body.get("total_commits"))
    if status == "identical":
        if base_sha != head_sha or total_commits != 0:
            raise RepoDiffRangeInvalid(
                "Identical comparison does not match the requested head"
            )
    else:
        commits = body.get("commits")
        if not isinstance(commits, list) or not commits:
            raise RepoDiffSourceUnavailable(
                "GitHub comparison is missing its final commit"
            )
        if _commit_sha(commits[-1]) != head_sha or total_commits == 0:
            raise RepoDiffRangeInvalid(
                "GitHub comparison head does not match the requested SHA"
            )
    files = body.get("files", [] if status == "identical" else None)
    if not isinstance(files, list):
        raise RepoDiffSourceUnavailable("GitHub comparison is missing its file list")
    if status == "identical" and files:
        raise RepoDiffSourceUnavailable(
            "Identical comparison unexpectedly includes files"
        )
    verify_on_main(head_sha, repo=repo, client=client, timeout=timeout)

    stats: list[str] = []
    patches: list[str] = []
    excluded = included = omitted = patch_available = additions = deletions = 0
    patch_chars = 0
    truncated = False
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            continue
        if _excluded(filename):
            excluded += 1
            continue
        included += 1
        added = _count(item.get("additions", 0))
        deleted = _count(item.get("deletions", 0))
        changes = _count(item.get("changes", added + deleted))
        additions += added
        deletions += deleted
        stats.append(f"{filename} | {changes} +{added} -{deleted}")
        hunk = item.get("patch")
        if not isinstance(hunk, str) or not hunk:
            omitted += 1
            continue
        patch_available += 1
        old = item.get("previous_filename")
        if not isinstance(old, str) or not old:
            old = filename
        old_path, new_path = _git_path("a", old), _git_path("b", filename)
        before = "/dev/null" if item.get("status") == "added" else old_path
        after = "/dev/null" if item.get("status") == "removed" else new_path
        hunk = hunk.rstrip("\n")
        patch = f"diff --git {old_path} {new_path}\n--- {before}\n+++ {after}\n{hunk}\n"
        if truncated or patch_chars + len(patch) > REPO_DIFF_PATCH_CAP:
            truncated = True
            continue
        patches.append(patch)
        patch_chars += len(patch)
    if truncated:
        while patches and patch_chars + len(_ELISION) + 1 > REPO_DIFF_PATCH_CAP:
            patch_chars -= len(patches.pop())
    patch_included = len(patches)
    patch = "".join(patches) + (f"{_ELISION}\n" if truncated else "")
    coverage = {
        "files_listed": len(files),
        "files_excluded": excluded,
        "files_included": included,
        "files_patch_included": patch_included,
        "files_patch_omitted_by_github": omitted,
        "files_patch_cut_by_cap": patch_available - patch_included,
        "patch_chars": len(patch),
        "patch_truncated": truncated,
        "file_list_complete": len(files) < 300,
        "total_commits": total_commits,
    }
    return RepoDiffEvidence(
        base_sha=base_sha,
        head_sha=head_sha,
        compare_status=status,
        total_commits=total_commits,
        diff_stat="\n".join(stats),
        patch=patch,
        changed_files=included,
        additions=additions,
        deletions=deletions,
        coverage=coverage,
    )
