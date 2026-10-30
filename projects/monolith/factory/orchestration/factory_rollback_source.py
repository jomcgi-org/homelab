"""Standard-library worker for a reserved rollback's ordinary factory task.

No merge, queue, cluster or credential operations. The task's normal worker
owns its checkout and must have passed the factory start/ownership fences.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess


def delivery_key(delivery):
    return hashlib.sha256(
        json.dumps(
            [delivery["repo"], delivery["task_id"], delivery["merge_commit_sha"]],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _run(checkout, *args):
    return subprocess.run(
        args, cwd=checkout, check=True, capture_output=True, text=True
    ).stdout.strip()


def reverse_source(checkout, delivery):
    key = delivery_key(delivery)

    def git(*args):
        return _run(checkout, "git", *args)

    branch = "factory/rollback-" + key[:32]
    if git("branch", "--show-current") != branch:
        raise ValueError("rollback source branch is not owned")
    if git("status", "--porcelain"):
        raise ValueError("rollback source worktree is dirty")
    merge = delivery["merge_commit_sha"]
    parents = git("rev-list", "--parents", "-n", "1", merge).split()[1:]
    if not parents:
        raise ValueError("cannot automatically revert a root commit")
    count = delivery.get("commit_count")
    if type(count) is not int or count < 1:
        raise ValueError("authoritative PR commit count required")
    if len(parents) > 1:
        commits = [merge]
    else:
        # The REBASE queue's merge SHA is the last commit of the landed range.
        base = git("rev-parse", f"{merge}~{count}")
        commits = git("rev-list", "--first-parent", f"{base}..{merge}").splitlines()
        if len(commits) != count or any(
            len(git("rev-list", "--parents", "-n", "1", sha).split()) != 2
            for sha in commits
        ):
            raise ValueError("landed range does not match PR commit count")
    marker = f"Factory-Rollback: {key}"
    lines = [f"This reverts commit {sha}." for sha in commits]
    # A formatter/rebase can add a newer commit. Find the complete range marker
    # in the branch's own commits, not only HEAD and not its source ancestry.
    messages = git("log", f"{merge}..HEAD", "--format=%B%x00").split("\x00")
    if any(
        marker in m.splitlines() and all(line in m.splitlines() for line in lines)
        for m in messages
    ):
        return {"head_sha": git("rev-parse", "HEAD"), "branch": branch}
    git("merge-base", "--is-ancestor", merge, "HEAD")
    command = ["revert", "--no-commit"]
    if len(parents) > 1:
        command.extend(["--mainline", "1"])
    git(*command, *commits)
    git(
        "commit",
        "-m",
        f"revert({delivery['chart']}): recover PR #{delivery['pr_number']}\n\n"
        + f"Reverts #{delivery['pr_number']}\n\n"
        + "\n".join(lines)
        + "\n\n"
        + marker,
    )
    return {"head_sha": git("rev-parse", "HEAD"), "branch": branch}


def validate_landed_range(checkout, delivery, source_commits):
    """Reject squash/foreign ranges instead of undoing unrelated main commits."""
    count = delivery["commit_count"]
    if len(source_commits) != count:
        raise ValueError("source PR commit inventory incomplete")
    merge = delivery["merge_commit_sha"]
    parents = _run(checkout, "git", "rev-list", "--parents", "-n", "1", merge).split()[
        1:
    ]
    if len(parents) > 1:
        return  # A true merge is reverted with -m 1, including its full tree diff.
    landed = _run(
        checkout,
        "git",
        "rev-list",
        "--reverse",
        "--first-parent",
        f"{merge}~{count}..{merge}",
    ).splitlines()

    def patch_id(sha):
        patch = _run(
            checkout,
            "git",
            "show",
            "--pretty=format:",
            "--binary",
            "--no-ext-diff",
            sha,
        )
        result = subprocess.run(
            ["git", "patch-id", "--stable"],
            cwd=checkout,
            input=patch,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.split()
        if not result:
            raise ValueError("empty PR commit cannot prove landed range")
        return result[0]

    if len(landed) != count or [patch_id(sha) for sha in landed] != [
        patch_id(sha) for sha in source_commits
    ]:
        raise ValueError("landed range is not the complete rebased PR")


def publish(checkout, delivery, recovery_issue):
    """Read back the deterministic branch/PR on every retry of the same work.

    GitHub enforces one open PR for this head/base. Closed PRs are adopted too,
    never replaced. Worker attempt fences prevent concurrent source writers.
    """
    repo = delivery["repo"]

    def gh(*args):
        return _run(checkout, "gh", *args)

    source = json.loads(gh("api", f"repos/{repo}/pulls/{delivery['pr_number']}"))
    if (
        source.get("merged") is not True
        or source.get("merge_commit_sha") != delivery["merge_commit_sha"]
    ):
        raise ValueError("source merge changed")
    if (
        type(source.get("commits")) is not int
        or source["commits"] != delivery["commit_count"]
    ):
        raise ValueError("source PR commit count changed")
    _run(checkout, "git", "fetch", "origin", "main")
    _run(checkout, "git", "fetch", "origin", f"refs/pull/{delivery['pr_number']}/head")
    if _run(checkout, "git", "rev-parse", "FETCH_HEAD") != source["head"]["sha"]:
        raise ValueError("source PR head changed")
    source_commits = []
    for page in range(1, 11):
        batch = json.loads(
            gh(
                "api",
                f"repos/{repo}/pulls/{delivery['pr_number']}/commits?per_page=100&page={page}",
            )
        )
        source_commits.extend(commit["sha"] for commit in batch)
        if len(batch) < 100:
            break
    validate_landed_range(checkout, delivery, source_commits)
    result = reverse_source(checkout, delivery)
    _run(checkout, "git", "push", "origin", f"HEAD:refs/heads/{result['branch']}")
    owner = repo.split("/", 1)[0]
    path = f"repos/{repo}/pulls?state=all&head={owner}:{result['branch']}&base=main&per_page=100"
    pulls = json.loads(gh("api", path))
    if len(pulls) > 1:
        raise ValueError("duplicate rollback pull requests")
    if not pulls:
        try:
            gh(
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                "main",
                "--head",
                result["branch"],
                "--title",
                f"revert({delivery['chart']}): recover PR #{delivery['pr_number']}",
                "--body",
                f"Reverts #{delivery['pr_number']}\n\nFixes #{recovery_issue}\n\nFactory-Rollback: {delivery_key(delivery)}\n\n"
                "Reverse the full landed source range. Required checks, independent exact-head review and the ordinary queue apply. No deployment pin edits.",
            )
        except subprocess.CalledProcessError:
            # An accepted create with a lost response or a racing retry is
            # recovered by the authoritative head/base read, never a new PR.
            pulls = json.loads(gh("api", path))
            if len(pulls) != 1:
                raise
        else:
            pulls = json.loads(gh("api", path))
    if len(pulls) != 1:
        raise ValueError("rollback PR creation outcome unknown")
    pull = json.loads(gh("api", f"repos/{repo}/pulls/{pulls[0]['number']}"))
    if (pull.get("head") or {}).get("sha") != result["head_sha"]:
        raise ValueError("rollback PR head moved; re-read before reviewing")
    return {**result, "pr_number": pull["number"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--delivery-json", required=True)
    parser.add_argument("--recovery-issue", required=True, type=int)
    args = parser.parse_args()
    print(
        json.dumps(
            publish(args.checkout, json.loads(args.delivery_json), args.recovery_issue)
        )
    )
