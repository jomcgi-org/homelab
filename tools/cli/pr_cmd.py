"""PR CLI subcommands: ``homelab pr land``.

Land a PR the way AGENTS.md prescribes, end to end: enqueue it on the merge
queue with bare ``gh pr merge --auto``, wait for the queue to merge or eject
it, wait for chart-version-bot's write-back, then poll the monolith's rollout
verdict (the REST twin of the ``verify_deployment`` MCP tool) until each
touched app is live. Needs ``gh`` authenticated for the repo and a Cloudflare
Access token for private.jomcgi.dev, like the other subcommands.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import typer

from tools.cli.scheduler_cmd import _request

REPO = "jomcgi-org/homelab"
POLL_SECS = 30
# ArgoCD caches HEAD for a few minutes, so a git-tracked app's verified verdict
# only proves a merge is live once it reconciled this long after the merge.
GIT_CACHE_GRACE = timedelta(minutes=5)

_WRITEBACK_BUMP = re.compile(r"^(\S+): (\S+) -> (\S+)$", re.MULTILINE)
_SOURCE_TRAILER = re.compile(r"^Chart-Source-Commit: ([0-9a-f]{40})$", re.MULTILINE)

_QUEUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      isDraft
      mergeStateStatus
      mergedAt
      mergeCommit { oid }
      autoMergeRequest { enabledAt }
      mergeQueueEntry { state position }
    }
  }
}
"""

pr_app = typer.Typer(
    name="pr",
    help="Land PRs through the merge queue and verify the rollout.",
    no_args_is_help=True,
)

# Indirection so tests can drive the clock without sleeping.
_sleep = time.sleep


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LandError(Exception):
    """A step failed in a way the caller must act on; the message says how."""


def _gh(*args: str) -> str:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=120, check=False
    )
    if proc.returncode != 0:
        raise LandError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout


def _gh_json(*args: str):
    return json.loads(_gh(*args))


@dataclass
class PrState:
    state: str
    is_draft: bool
    merge_state: str
    merged_at: str | None
    merge_commit: str | None
    auto_merge: bool
    queue_state: str | None
    queue_position: int | None

    def describe(self) -> str:
        if self.state == "MERGED":
            return f"merged as {(self.merge_commit or '')[:9]}"
        if self.queue_state:
            return f"in merge queue: {self.queue_state} (position {self.queue_position})"
        if self.auto_merge:
            return f"auto-merge armed, waiting on checks ({self.merge_state})"
        return f"{self.state.lower()}, not queued ({self.merge_state})"


def _pr_state(number: int, repo: str) -> PrState:
    owner, name = repo.split("/", 1)
    data = _gh_json(
        "api",
        "graphql",
        "-f",
        f"query={_QUEUE_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={number}",
    )
    pr = data["data"]["repository"]["pullRequest"]
    entry = pr.get("mergeQueueEntry") or {}
    return PrState(
        state=pr["state"],
        is_draft=pr["isDraft"],
        merge_state=pr["mergeStateStatus"],
        merged_at=pr.get("mergedAt"),
        merge_commit=(pr.get("mergeCommit") or {}).get("oid"),
        auto_merge=pr.get("autoMergeRequest") is not None,
        queue_state=entry.get("state"),
        queue_position=entry.get("position"),
    )


def _waiting(s: PrState) -> bool:
    return s.queue_state is not None or s.auto_merge


def _enqueue(number: int, repo: str, s: PrState) -> None:
    if s.state == "CLOSED":
        raise LandError(f"#{number} is closed without merging")
    if s.is_draft:
        raise LandError(f"#{number} is a draft: mark it ready (gh pr ready {number})")
    if s.merge_state == "DIRTY":
        raise LandError(
            f"#{number} conflicts with main: rebase it yourself and push, "
            "since --auto silently enqueues nothing while it is DIRTY"
        )
    if _waiting(s):
        return
    # Bare --auto: the queue sets the strategy and refuses --rebase.
    _gh("pr", "merge", str(number), "--auto", "--repo", repo)


def wait_for_merge(number: int, repo: str, deadline: datetime) -> PrState:
    """Enqueue if needed, then poll until merged; raise if ejected or timed out."""
    s = _pr_state(number, repo)
    if s.state != "MERGED":
        _enqueue(number, repo, s)
    last = ""
    grace = 2  # polls for GitHub to show the enqueue before calling it ejected
    while True:
        s = _pr_state(number, repo)
        line = s.describe()
        if line != last:
            typer.echo(f"#{number}: {line}")
            last = line
        if s.state == "MERGED":
            return s
        if s.state == "CLOSED":
            raise LandError(f"#{number} was closed without merging")
        if not _waiting(s):
            if grace <= 0:
                raise LandError(
                    f"#{number} left the merge queue without merging "
                    f"({s.merge_state}): read the failed run "
                    f"(gh pr checks {number}; docs/agents/ci-triage.md), fix it, "
                    "then land again"
                )
            grace -= 1
        if _now() >= deadline:
            raise LandError(f"timed out waiting for #{number} to merge ({line})")
        _sleep(POLL_SECS)


def infer_apps(files: list[str], repo: str) -> list[str]:
    """Apps whose ``projects/<app>/`` or ``projects/gke-apps/<app>/`` the PR touched.

    An app is a directory under ``projects/gke-apps/`` (the hub Applications),
    and every one of them is named after its directory.
    """
    known = set(
        _gh(
            "api",
            f"repos/{repo}/contents/projects/gke-apps",
            "--jq",
            '.[] | select(.type == "dir") | .name',
        ).split()
    )
    touched = set()
    for path in files:
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "projects":
            continue
        name = parts[2] if parts[1] == "gke-apps" else parts[1]
        if name in known:
            touched.add(name)
    return sorted(touched)


def _descends_from(repo: str, base: str, head: str) -> bool:
    if base == head:
        return True
    status = _gh(
        "api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".status"
    ).strip()
    return status in {"ahead", "identical"}


def find_writeback(repo: str, merge_commit: str, merged_at: str) -> dict[str, str] | None:
    """Chart bumps (``chart dir -> new version``) from the write-back covering a merge.

    Returns None while no chart-version-bot commit has a ``Chart-Source-Commit``
    at or after ``merge_commit``. An empty dict means one landed and bumped
    nothing this merge could have changed.
    """
    commits = _gh_json(
        "api",
        f"repos/{repo}/commits?sha=main&since={merged_at}&per_page=100",
    )
    # The API lists newest first; the oldest covering write-back is ours.
    for commit in reversed(commits):
        message = commit["commit"]["message"]
        source = _SOURCE_TRAILER.search(message)
        if not source or not _descends_from(repo, merge_commit, source.group(1)):
            continue
        return {m.group(1): m.group(3) for m in _WRITEBACK_BUMP.finditer(message)}
    return None


def _verdict(app: str, expected: str | None) -> dict:
    params = {"expected_revision": expected} if expected else {}
    resp = _request("get", f"/api/cluster/applications/{app}/verdict", params=params)
    resp.raise_for_status()
    return resp.json()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _failing_checks(result: dict) -> str:
    return "; ".join(
        f"{c['name']}: {c['detail']}"
        for c in result.get("checks", [])
        if c.get("state") != "verified"
    )


def wait_for_rollout(
    app: str, expected: str | None, merged_at: datetime, deadline: datetime
) -> dict:
    """Poll the verdict until verified or failed; raise on failure or timeout."""
    last = ""
    while True:
        result = _verdict(app, expected)
        if "error" in result:
            raise LandError(f"{app}: {result['error']}")
        verdict = result.get("verdict")
        if verdict == "verified" and expected is None:
            reconciled = _parse_time(result.get("reconciled_at"))
            if reconciled is None or reconciled < merged_at + GIT_CACHE_GRACE:
                verdict = "in_progress"
                result.setdefault("checks", []).append(
                    {
                        "name": "reconciled_at",
                        "state": "in_progress",
                        "detail": "not yet reconciled 5 minutes past the merge",
                    }
                )
        line = f"{app}: {verdict}"
        if verdict != "verified":
            line += f" ({_failing_checks(result)})"
        if line != last:
            typer.echo(line)
            last = line
        if verdict == "verified":
            return result
        if verdict == "failed":
            raise LandError(f"{app} rollout failed: {_failing_checks(result)}")
        if _now() >= deadline:
            raise LandError(f"timed out waiting for {app} to roll out")
        _sleep(POLL_SECS)


@pr_app.command("land")
def land(
    number: int = typer.Argument(..., help="PR number"),
    app: list[str] = typer.Option(
        None,
        "--app",
        help="ArgoCD Application to verify (repeatable). Default: inferred "
        "from the paths the PR touched.",
    ),
    no_verify: bool = typer.Option(
        False, "--no-verify", help="Stop once merged; skip the rollout check."
    ),
    timeout_min: int = typer.Option(
        90, "--timeout", help="Minutes to wait overall before giving up."
    ),
    repo: str = typer.Option(REPO, "--repo", help="owner/name"),
) -> None:
    """Enqueue a PR, wait for the queue to merge it, then verify it is live."""
    deadline = _now() + timedelta(minutes=timeout_min)
    try:
        merged = wait_for_merge(number, repo, deadline)
        if no_verify:
            return
        if not app:
            files = _gh_json(
                "pr", "view", str(number), "--repo", repo, "--json", "files"
            )["files"]
            app = infer_apps([f["path"] for f in files], repo)
        apps = app
        if not apps:
            typer.echo(f"#{number}: touches no deployed app; nothing to verify.")
            return
        merged_at = _parse_time(merged.merged_at) or _now()

        typer.echo(f"#{number}: waiting for chart-version-bot's write-back")
        bumps = None
        while bumps is None:
            bumps = find_writeback(repo, merged.merge_commit or "", merged.merged_at)
            if bumps is None:
                if _now() >= deadline:
                    raise LandError("timed out waiting for the chart write-back")
                _sleep(POLL_SECS)

        for name in apps:
            expected = bumps.get(f"projects/{name}/chart")
            target = f"chart {expected}" if expected else "git HEAD"
            typer.echo(f"{name}: verifying {target}")
            wait_for_rollout(name, expected, merged_at, deadline)
        typer.echo(f"#{number}: landed and live in {', '.join(apps)}.")
    except LandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
