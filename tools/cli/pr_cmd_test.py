"""Tests for tools/cli/pr_cmd.py: ``homelab pr land``."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from tools.cli import pr_cmd
from tools.cli.main import app

MERGE = "a" * 40
SOURCE = "b" * 40
MERGED_AT = "2026-09-29T12:00:00Z"
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _pr(state="OPEN", *, queue=None, auto=False, merge_state="CLEAN", draft=False):
    merged = state == "MERGED"
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": state,
                    "isDraft": draft,
                    "mergeStateStatus": merge_state,
                    "mergedAt": MERGED_AT if merged else None,
                    "mergeCommit": {"oid": MERGE} if merged else None,
                    "autoMergeRequest": {"enabledAt": "x"} if auto else None,
                    "mergeQueueEntry": {"state": queue, "position": 1}
                    if queue
                    else None,
                }
            }
        }
    }


WRITEBACK = {
    "commit": {
        "message": (
            "chore(charts): publish 1 chart version(s)\n\n"
            "projects/monolith/chart: 0.558.10 -> 0.559.0\n\n"
            f"Chart-Source-Commit: {SOURCE}\n"
            "Chart-Published: projects/monolith/chart 0.559.0\n"
        )
    }
}


class FakeGh:
    """Scripted ``gh``: each GraphQL poll pops the next PR state."""

    def __init__(self, states, *, files=(), commits=None, compare="ahead"):
        self.states = list(states)
        self.files = list(files)
        self.commits = commits if commits is not None else [WRITEBACK]
        self.compare = compare
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("api", "graphql"):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return json.dumps(state)
        if args[:2] == ("pr", "merge"):
            return ""
        if args[:2] == ("pr", "view"):
            return json.dumps({"files": [{"path": p} for p in self.files]})
        if args[0] == "api" and args[1].endswith("contents/projects/gke-apps"):
            return "embervm\nembervm-dev\nmonolith\nmonolith-public\n"
        if args[0] == "api" and "/commits?" in args[1]:
            return json.dumps(self.commits)
        if args[0] == "api" and "/compare/" in args[1]:
            return self.compare + "\n"
        raise AssertionError(f"unexpected gh call {args}")


class FakeResp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


@pytest.fixture()
def clock(monkeypatch):
    now = {"t": T0}

    def _sleep(secs):
        now["t"] += timedelta(seconds=secs)

    monkeypatch.setattr(pr_cmd, "_sleep", _sleep)
    monkeypatch.setattr(pr_cmd, "_now", lambda: now["t"])
    return now


def _install(monkeypatch, gh, verdicts=()):
    monkeypatch.setattr(pr_cmd, "_gh", gh)
    queue = list(verdicts)
    seen = []

    def _request(method, path, params=None):
        seen.append((path, params))
        return FakeResp(queue.pop(0) if len(queue) > 1 else queue[0])

    monkeypatch.setattr(pr_cmd, "_request", _request)
    return seen


def _verified(**extra):
    return {"verdict": "verified", "checks": [], **extra}


def test_enqueues_with_bare_auto_then_verifies_the_written_back_chart(
    monkeypatch, clock
):
    gh = FakeGh(
        [_pr(), _pr(auto=True), _pr(queue="AWAITING_CHECKS"), _pr("MERGED")],
        files=["projects/monolith/cluster/router.py"],
    )
    seen = _install(
        monkeypatch,
        gh,
        [{"verdict": "in_progress", "checks": []}, _verified()],
    )

    result = CliRunner().invoke(app, ["pr", "land", "42"])

    assert result.exit_code == 0, result.output
    assert ("pr", "merge", "42", "--auto", "--repo", pr_cmd.REPO) in gh.calls
    assert not any("--rebase" in call for call in gh.calls)
    assert seen[-1] == (
        "/api/cluster/applications/monolith/verdict",
        {"expected_revision": "0.559.0"},
    )
    assert "landed and live in monolith" in result.output


def test_does_not_re_enqueue_a_pr_already_in_the_queue(monkeypatch, clock):
    gh = FakeGh([_pr(queue="QUEUED"), _pr("MERGED")])
    _install(monkeypatch, gh, [_verified()])

    result = CliRunner().invoke(app, ["pr", "land", "7", "--no-verify"])

    assert result.exit_code == 0, result.output
    assert not any(call[:2] == ("pr", "merge") for call in gh.calls)


def test_dirty_pr_is_refused_with_rebase_advice(monkeypatch, clock):
    gh = FakeGh([_pr(merge_state="DIRTY")])
    _install(monkeypatch, gh, [_verified()])

    result = CliRunner().invoke(app, ["pr", "land", "7"])

    assert result.exit_code == 1
    assert "rebase" in result.output
    assert not any(call[:2] == ("pr", "merge") for call in gh.calls)


def test_ejection_from_the_queue_fails_and_points_at_ci_triage(monkeypatch, clock):
    gh = FakeGh(
        [_pr(), _pr(queue="AWAITING_CHECKS"), _pr(merge_state="BLOCKED")],
    )
    _install(monkeypatch, gh, [_verified()])

    result = CliRunner().invoke(app, ["pr", "land", "7"])

    assert result.exit_code == 1
    assert "left the merge queue" in result.output
    assert "ci-triage" in result.output


def test_git_app_waits_for_reconcile_past_argocd_cache(monkeypatch, clock):
    gh = FakeGh([_pr("MERGED")], files=["projects/gke-apps/monolith-public/x.yaml"])
    gh.commits = [
        {
            "commit": {
                "message": f"chore(charts): publish 0\n\nChart-Source-Commit: {SOURCE}\n"
            }
        }
    ]
    early = (T0 + timedelta(minutes=1)).isoformat()
    late = (T0 + timedelta(minutes=6)).isoformat()
    seen = _install(
        monkeypatch,
        gh,
        [_verified(reconciled_at=early), _verified(reconciled_at=late)],
    )

    result = CliRunner().invoke(app, ["pr", "land", "7"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 2
    assert seen[0] == ("/api/cluster/applications/monolith-public/verdict", {})


def test_failed_rollout_exits_nonzero_with_the_failing_check(monkeypatch, clock):
    gh = FakeGh([_pr("MERGED")])
    _install(
        monkeypatch,
        gh,
        [
            {
                "verdict": "failed",
                "checks": [{"name": "health", "state": "failed", "detail": "Degraded"}],
            }
        ],
    )

    result = CliRunner().invoke(app, ["pr", "land", "7", "--app", "monolith"])

    assert result.exit_code == 1
    assert "health: Degraded" in result.output


def test_writeback_older_than_the_merge_is_ignored(monkeypatch, clock):
    gh = FakeGh([_pr("MERGED")], compare="behind")
    _install(monkeypatch, gh, [_verified()])

    result = CliRunner().invoke(
        app, ["pr", "land", "7", "--app", "monolith", "--timeout", "1"]
    )

    assert result.exit_code == 1
    assert "timed out waiting for the chart write-back" in result.output


def test_pr_touching_no_app_stops_after_merge(monkeypatch, clock):
    gh = FakeGh([_pr("MERGED")], files=["docs/agents/ship.md", "tools/cli/main.py"])
    seen = _install(monkeypatch, gh, [_verified()])

    result = CliRunner().invoke(app, ["pr", "land", "7"])

    assert result.exit_code == 0, result.output
    assert "nothing to verify" in result.output
    assert seen == []


def test_infer_apps_maps_project_and_hub_paths(monkeypatch):
    monkeypatch.setattr(pr_cmd, "_gh", FakeGh([_pr()]))

    apps = pr_cmd.infer_apps(
        [
            "projects/embervm/chart/values.yaml",
            "projects/gke-apps/monolith/application.yaml",
            "projects/mcp/thing.py",
            "README.md",
        ],
        pr_cmd.REPO,
    )

    assert apps == ["embervm", "monolith"]
