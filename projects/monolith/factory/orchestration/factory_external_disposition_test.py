"""Hermetic tests for the externally-fulfilled terminal disposition.

Every GitHub read is faked through monkeypatched module readers, so these
cases run with no cluster and no network.
"""

import pytest

from factory.orchestration import factory_external_disposition as external

HEAD = "b" * 40
TASK = {
    "id": "t-1",
    "repo": "owner/repo",
    "base_branch": "main",
    "conductor_model": "astra",
    "issue_number": 77,
}


def _pr(head=HEAD, ref="external/fix-77", **overrides):
    pr = {
        "state": "closed",
        "merged": True,
        "merged_at": "2026-09-20T00:00:00Z",
        "body": "Delivers the fix.\n\nCloses #77",
        "head": {
            "sha": head,
            "ref": ref,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        "html_url": "https://github.com/owner/repo/pull/5921",
    }
    pr.update(overrides)
    return pr


def test_happy_path_returns_provenance_evidence(monkeypatch):
    pr = _pr()
    checks = {"state": "success", "statuses": [{"context": "pr-checks", "state": "success"}]}
    reviews = [{"state": "APPROVED", "commit_id": HEAD}]
    monkeypatch.setattr(
        "factory.orchestration.factory_conductor.github_get",
        lambda _repo, path: pr if path.startswith("pulls/") else checks,
    )
    monkeypatch.setattr(
        "factory.orchestration.factory_conductor.github_list",
        lambda _repo, _path: reviews,
    )
    evidence = external.verify_external_disposition(TASK, 5921, HEAD)
    assert evidence == {
        "pr_url": "https://github.com/owner/repo/pull/5921",
        "head_sha": HEAD,
        "state": "externally_fulfilled",
        "reason": (
            f"externally fulfilled by merged PR #5921 at {HEAD}; "
            "factory delivery not claimed"
        ),
    }


def _run(monkeypatch, task=None, number=5921, head=HEAD, pr=None, reviews=None,
         check_state="success"):
    task = TASK if task is None else task
    pr = _pr() if pr is None else pr
    reviews = (
        [{"state": "APPROVED", "commit_id": HEAD}] if reviews is None else reviews
    )
    checks = {
        "state": check_state,
        "statuses": [{"context": "pr-checks", "state": check_state}],
    }
    monkeypatch.setattr(
        "factory.orchestration.factory_conductor.github_get",
        lambda _repo, path: pr if path.startswith("pulls/") else checks,
    )
    monkeypatch.setattr(
        "factory.orchestration.factory_conductor.github_list",
        lambda _repo, _path: reviews,
    )
    return external.verify_external_disposition(task, number, head)


def test_open_pr_settles_nothing(monkeypatch):
    pr = _pr()
    pr.update({"state": "open", "merged": False, "merged_at": None})
    with pytest.raises(ValueError, match="not merged"):
        _run(monkeypatch, pr=pr)


def test_stale_head_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="not the external PR head"):
        _run(monkeypatch, head="c" * 40)


def test_task_branch_pr_must_use_finish(monkeypatch):
    pr = _pr(ref="factory/t-1")
    with pytest.raises(ValueError, match="task branch"):
        _run(monkeypatch, pr=pr)


def test_pr_that_does_not_close_the_issue_covers_no_scope(monkeypatch):
    pr = _pr()
    pr["body"] = "Unrelated cleanup with no keyword."
    with pytest.raises(ValueError, match="does not close"):
        _run(monkeypatch, pr=pr)


def test_unpassed_checks_block_settlement(monkeypatch):
    with pytest.raises(ValueError, match="required checks"):
        _run(monkeypatch, check_state="failure")


def test_approval_at_another_commit_is_no_evidence(monkeypatch):
    reviews = [{"state": "APPROVED", "commit_id": "c" * 40}]
    with pytest.raises(ValueError, match="no approving review"):
        _run(monkeypatch, reviews=reviews)


def test_changes_requested_only_is_no_evidence(monkeypatch):
    reviews = [{"state": "CHANGES_REQUESTED", "commit_id": HEAD}]
    with pytest.raises(ValueError, match="no approving review"):
        _run(monkeypatch, reviews=reviews)


def test_wrong_base_branch_is_rejected(monkeypatch):
    pr = _pr()
    pr["base"] = {"ref": "other", "repo": {"full_name": "owner/repo"}}
    with pytest.raises(ValueError, match="base branch"):
        _run(monkeypatch, pr=pr)


def test_malformed_head_is_rejected_before_any_read(monkeypatch):
    def explode(_repo, _path):  # pragma: no cover
        raise AssertionError("no GitHub read should happen")

    monkeypatch.setattr(
        "factory.orchestration.factory_conductor.github_get", explode
    )
    with pytest.raises(ValueError, match="exact head"):
        external.verify_external_disposition(TASK, 5921, "short")
