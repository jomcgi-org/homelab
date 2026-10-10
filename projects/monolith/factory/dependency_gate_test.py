"""All GitHub evidence is mocked, including queue discovery and publication."""

import json
from copy import deepcopy

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, create_engine

from factory import dependency_gate as gate
from factory.orchestration import dependency_prs as deps
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

REPO = "owner/repo"
HEAD, BASE, QUEUE_BASE, QUEUE_HEAD = (letter * 40 for letter in "abcd")
POLICY = {"repo": REPO, "base_branch": "main"}
APP_ID = 456


def pull(*, kind="Bot", ref="dependabot/pip/example-2", number=91):
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "changed_files": 1,
        "user": {"id": 123, "login": "dependency-bot", "type": kind},
        "head": {"sha": HEAD, "ref": ref, "repo": {"full_name": REPO}},
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPO}},
    }


def queue_entry(number=91, position=1):
    return {
        "id": f"entry-{number}",
        "position": position,
        "baseCommit": {"oid": QUEUE_BASE},
        "headCommit": {"oid": QUEUE_HEAD},
        "pullRequest": {"number": number, "headRefOid": HEAD},
    }


class FakeGitHub:
    def __init__(self):
        self.pull = pull()
        self.files = [
            {
                "filename": "requirements.txt",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "sha": "e" * 40,
            }
        ]
        self.changes = [
            {
                "change_type": "added",
                "name": "example",
                "version": "2.0",
                "vulnerabilities": [],
            }
        ]
        self.alerts = []
        self.entries = [queue_entry()]
        self.comparison = None
        self.error = None
        self.reads = []
        self.check_runs = []
        self.check_error = None

    def get(self, repo, endpoint):
        assert repo == REPO
        self.reads.append(endpoint)
        if "/check-runs?" in endpoint:
            if self.check_error:
                raise self.check_error
            rows = [
                run
                for run in self.check_runs
                if run["head_sha"] == endpoint.split("/")[1]
            ]
            return {
                "total_count": len(rows),
                "check_runs": deepcopy(rows),
            }
        if endpoint.startswith("pulls/"):
            return deepcopy(self.pull)
        assert endpoint == f"compare/{QUEUE_BASE}...{QUEUE_HEAD}"
        return deepcopy(
            self.comparison
            or {
                "files": self.files,
                "status": "ahead",
                "base_commit": {"sha": QUEUE_BASE},
                "merge_base_commit": {"sha": QUEUE_BASE},
            }
        )

    def rows(self, repo, endpoint):
        assert repo == REPO
        self.reads.append(endpoint)
        if self.error:
            raise self.error
        if endpoint.startswith("pulls?"):
            return [deepcopy(self.pull)]
        if "/files?" in endpoint:
            return deepcopy(self.files)
        if endpoint.startswith("dependency-graph/"):
            return deepcopy(self.changes)
        if endpoint.startswith("dependabot/alerts?"):
            return deepcopy(self.alerts)
        raise AssertionError(endpoint)

    def queue(self, repo, branch):
        assert (repo, branch) == (REPO, "main")
        return deepcopy(self.entries)


def runs(evidence):
    assessment = {
        "safe": True,
        "base_sha": evidence["base_sha"],
        "evidence_sha256": evidence["evidence_sha256"],
        **{
            dimension: "Inspected upstream source and validated on isolated Linux: pass."
            for dimension in deps.ASSESSMENTS
        },
    }
    result = []
    for index, node in enumerate(("implement_security", "review_security"), 1):
        value = {
            "pr_number": 91,
            "head_sha": HEAD,
            "dependency_assessment": deepcopy(assessment),
            **({"status": "complete"} if index == 1 else {"verdict": "approve"}),
        }
        result.append(
            {
                "id": index,
                "node_key": node,
                "status": "succeeded",
                "head_sha": HEAD,
                "session_id": index + 10,
                "pin": {
                    "read_only": True,
                    "dependency_investigations": ["implement_security"],
                },
                "outcome_json": json.dumps(
                    {
                        "value": value,
                        "artifact": {"status": "ok", "errors": [], "value": value},
                    }
                ),
            }
        )
    return result


@pytest.fixture
def setup(monkeypatch):
    # Any forgotten mock is a test failure, never a real GitHub request.
    monkeypatch.setattr(
        httpx, "Client", lambda **_kw: pytest.fail("unexpected network")
    )
    github = FakeGitHub()
    evidence = deps.snapshot(
        REPO, github.pull, read_get=github.get, read_list=github.rows
    )
    approved = gate.Approval(7, "task-review", evidence, runs(evidence))
    monkeypatch.setattr(gate, "approval", lambda *_args: approved)
    audits, publications = [], []
    monkeypatch.setattr(
        gate, "audit", lambda action, **detail: audits.append((action, detail))
    )

    def post(repo, endpoint, payload, token):
        publications.append((repo, endpoint, payload, token))
        body = {"id": len(publications), "app": {"id": APP_ID}, **payload}
        github.check_runs.append(body)
        return body

    monkeypatch.setattr(gate, "_github_post", post)
    monkeypatch.setenv(gate.ENABLED_ENV, "true")
    monkeypatch.setenv(gate.TOKEN_ENV, "dedicated-app-token")
    monkeypatch.setenv("FACTORY_DEPENDENCY_GATE_APP_ID", str(APP_ID))
    monkeypatch.setenv("GITHUB_API_TOKEN", "must-never-be-used")
    monkeypatch.setattr(gate, "GitHub", lambda token: github)
    return github, approved, audits, publications


@pytest.mark.parametrize("flag", [None, "false", "1", "yes"])
def test_disabled_publisher_is_a_pure_noop(setup, monkeypatch, flag):
    github, _, audits, publications = setup
    github.reads.clear()
    if flag is None:
        monkeypatch.delenv(gate.ENABLED_ENV)
    else:
        monkeypatch.setenv(gate.ENABLED_ENV, flag)
    for name in ("_read_session", "_locked_session"):
        monkeypatch.setattr(
            gate, name, lambda: pytest.fail("disabled gate opened a session")
        )
    assert gate.tick(POLICY) == {"action": "skipped", "reason": "publisher_disabled"}
    assert not github.reads and not publications
    assert not audits


def test_missing_token_does_not_fall_back_to_api_token(setup, monkeypatch):
    github, _, audits, publications = setup
    github.reads.clear()
    monkeypatch.delenv(gate.TOKEN_ENV)
    assert gate.tick(POLICY)["reason"] == "publisher_token_missing"
    assert gate.tick(POLICY)["reason"] == "publisher_token_missing"
    assert not github.reads and not publications
    assert not audits


def test_unchanged_tick_revalidates_without_republishing(setup):
    github, _, _, publications = setup
    assert gate.tick(POLICY)["action"] == "checked"
    github.reads.clear()
    assert gate.tick(POLICY)["action"] == "checked"
    assert len(publications) == 2
    assert f"dependency-graph/compare/{QUEUE_BASE}...{QUEUE_HEAD}" in github.reads


def test_changed_evidence_revokes_published_success(setup):
    github, _, audits, publications = setup
    gate.tick(POLICY)
    github.changes[0]["version"] = "3.0"
    gate.tick(POLICY)
    assert [item[2]["conclusion"] for item in publications] == [
        "success",
        "success",
        "failure",
        "failure",
    ]
    assert any(action == "dependency_approval_invalidated" for action, _ in audits)


def test_existing_check_read_failure_publishes(setup):
    github, _, _, publications = setup
    gate.tick(POLICY)
    github.check_error = httpx.ConnectError("check read unavailable")
    gate.tick(POLICY)
    assert len(publications) == 4


def test_other_app_run_does_not_suppress_publication(setup):
    github, _, _, publications = setup
    gate.tick(POLICY)
    for run in github.check_runs:
        run["app"]["id"] = APP_ID + 1
    gate.tick(POLICY)
    assert len(publications) == 4


@pytest.mark.parametrize("app_id", [None, "", "0", "invalid"])
def test_unknown_app_identity_always_publishes(setup, monkeypatch, app_id):
    _, _, _, publications = setup
    if app_id is None:
        monkeypatch.delenv("FACTORY_DEPENDENCY_GATE_APP_ID")
    else:
        monkeypatch.setenv("FACTORY_DEPENDENCY_GATE_APP_ID", app_id)
    gate.tick(POLICY)
    gate.tick(POLICY)
    assert len(publications) == 4


@pytest.mark.parametrize(
    "malformed",
    [
        "missing",
        "not_list",
        "incomplete",
        "bad_id",
        "bad_app",
        "bad_head",
        "bad_name",
        "bad_status",
        "bad_conclusion",
        "duplicate",
    ],
)
def test_malformed_check_inventory_always_publishes(setup, monkeypatch, malformed):
    github, _, _, publications = setup
    gate.tick(POLICY)
    run = deepcopy(github.check_runs[0])
    body = {"total_count": 1, "check_runs": [run]}
    if malformed == "missing":
        del body["total_count"]
    elif malformed == "not_list":
        body["check_runs"] = None
    elif malformed == "incomplete":
        body["total_count"] = 101
    elif malformed == "duplicate":
        body["check_runs"].append(run)
        body["total_count"] = 2
    else:
        key = {
            "bad_id": "id",
            "bad_app": "app",
            "bad_head": "head_sha",
            "bad_name": "name",
            "bad_status": "status",
            "bad_conclusion": "conclusion",
        }[malformed]
        run[key] = None
    monkeypatch.setattr(github, "get", lambda *_: body)
    gate.publish(REPO, HEAD, "success", "safe", "token")
    assert len(publications) == 3


def test_latest_owned_run_controls_deduplication(setup):
    github, _, _, publications = setup
    gate.publish(REPO, HEAD, "success", "safe", "token")
    gate.publish(REPO, HEAD, "failure", "unavailable", "token")
    gate.publish(REPO, HEAD, "success", "restored", "token")
    # A newer foreign refusal cannot hide this App's latest success.
    github.check_runs.append(
        {
            **deepcopy(github.check_runs[-1]),
            "id": 999,
            "app": {"id": APP_ID + 1},
            "conclusion": "failure",
        }
    )
    github.check_runs.reverse()
    gate.publish(REPO, HEAD, "success", "still safe", "token")
    assert len(publications) == 3


def test_pending_owned_run_does_not_suppress_publication(setup):
    github, _, _, publications = setup
    gate.publish(REPO, HEAD, "success", "safe", "token")
    github.check_runs[-1].update(status="in_progress", conclusion=None)
    gate.publish(REPO, HEAD, "success", "safe", "token")
    assert len(publications) == 2


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("unavailable"),
        httpx.ReadTimeout("timeout"),
        httpx.HTTPStatusError(
            "5xx",
            request=httpx.Request("GET", "https://api.github.com"),
            response=httpx.Response(503),
        ),
    ],
)
def test_unavailable_evidence_refuses_without_invalidating(setup, error):
    github, _, audits, publications = setup
    github.error = error
    gate.check(REPO, "main", github.pull, POLICY, github, "token", [queue_entry()])
    assert publications[-1][2]["conclusion"] == "failure"
    assert not any(action == "dependency_approval_invalidated" for action, _ in audits)
    github.error = None
    gate.check(REPO, "main", github.pull, POLICY, github, "token", [queue_entry()])
    assert publications[-1][2]["conclusion"] == "success"


@pytest.mark.parametrize("ref", ["human-fix", "factory/t-issue"])
def test_human_and_factory_issue_prs_pass_without_dependency_approval(
    setup, ref, monkeypatch
):
    github, _, _, publications = setup
    github.pull = pull(kind="User", ref=ref)
    monkeypatch.setattr(
        gate, "approval", lambda *_args: pytest.fail("human approval lookup")
    )
    assert gate.tick(POLICY)["action"] == "checked"
    assert {entry[2]["conclusion"] for entry in publications} == {"success"}
    assert {entry[2]["head_sha"] for entry in publications} == {HEAD, QUEUE_HEAD}


def test_approved_dependency_passes_pr_and_queue_with_matching_content(setup):
    github, _, audits, publications = setup
    assert gate.tick(POLICY)["action"] == "checked"
    assert [entry[2]["conclusion"] for entry in publications] == ["success", "success"]
    assert {entry[3] for entry in publications} == {"dedicated-app-token"}
    assert f"dependency-graph/compare/{QUEUE_BASE}...{QUEUE_HEAD}" in github.reads
    assert sum(action == "dependency_gate_published" for action, _ in audits) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "malformed_graph",
        "empty_graph",
        "truncated_graph",
        "truncated_files",
        "malformed_files",
        "unsafe_graph",
        "new_advisory",
        "malformed_alert",
        "truncated_alerts",
        "moved_pr",
        "new_author",
        "queued_head_moved",
        "queued_base_differs",
        "queued_blob_differs",
        "truncated_compare",
        "wrong_merge_base",
        "missing_worker",
        "missing_reviewer",
        "same_session",
        "not_independent",
        "unsafe",
        "stale_digest",
        "stale_base",
        "stale_head",
        "bad_artifact",
        "unapproved",
        "failed_run",
        "missing_dimension",
        "bad_receipt_digest",
        "old_receipt_files",
    ],
)
def test_dependency_failure_refuses_with_invalidation_only_on_positive_evidence(
    setup, failure
):
    github, approved, audits, publications = setup
    original = deepcopy(github.pull)
    entry = queue_entry()
    if failure == "malformed_graph":
        github.changes = [{"name": "example"}]
    elif failure == "empty_graph":
        github.changes = []
    elif failure == "truncated_graph":
        github.changes *= deps.MAX_ROWS
    elif failure == "truncated_files":
        github.files *= 100
    elif failure == "malformed_files":
        github.files = [None]
    elif failure == "unsafe_graph":
        github.changes[0]["vulnerabilities"] = [{"severity": "high"}]
    elif failure in ("new_advisory", "truncated_alerts"):
        github.alerts = [
            {
                "number": 1,
                "dependency": {"package": {"name": "example"}},
                "security_advisory": {"ghsa_id": "GHSA-example"},
                "security_vulnerability": {"severity": "high"},
            }
        ]
        if failure == "truncated_alerts":
            github.alerts *= 100
    elif failure == "malformed_alert":
        github.alerts = [{"number": 1}]
    elif failure == "moved_pr":
        github.pull["head"]["sha"] = "f" * 40
    elif failure == "new_author":
        github.pull["user"]["id"] = 999
    elif failure == "queued_head_moved":
        entry["pullRequest"]["headRefOid"] = "f" * 40
    elif failure == "queued_base_differs":
        github.changes[0]["version"] = "3.0"
    elif failure == "queued_blob_differs":
        github.files[0]["sha"] = "f" * 40
    elif failure == "truncated_compare":
        github.comparison = {"files": github.files * 300}
    elif failure == "wrong_merge_base":
        github.comparison = {
            "files": github.files,
            "base_commit": {"sha": QUEUE_BASE},
            "merge_base_commit": {"sha": BASE},
            "status": "ahead",
        }
    elif failure == "missing_worker":
        approved.runs.pop(0)
    elif failure == "missing_reviewer":
        approved.runs.pop()
    elif failure == "same_session":
        approved.runs[1]["session_id"] = approved.runs[0]["session_id"]
    elif failure == "not_independent":
        approved.runs[1]["pin"]["dependency_investigations"] = []
    elif failure == "failed_run":
        approved.runs[1]["status"] = "failed"
    elif failure == "bad_receipt_digest":
        approved.evidence["evidence_sha256"] = "f" * 64
    elif failure == "old_receipt_files":
        # Recompute the receipt digest, as for a legitimate pre-gate receipt.
        approved.evidence["files"][0]["sha"] = None
        import hashlib

        encoded = json.dumps(
            {k: v for k, v in approved.evidence.items() if k != "evidence_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        )
        approved.evidence["evidence_sha256"] = hashlib.sha256(
            encoded.encode()
        ).hexdigest()
        approved.runs[:] = runs(approved.evidence)
    else:
        run = approved.runs[1]
        outcome = json.loads(run["outcome_json"])
        value = outcome["value"]
        assessment = value["dependency_assessment"]
        if failure == "unsafe":
            assessment["safe"] = False
        elif failure == "stale_digest":
            assessment["evidence_sha256"] = "f" * 64
        elif failure == "stale_base":
            assessment["base_sha"] = "f" * 40
        elif failure == "stale_head":
            value["head_sha"] = "f" * 40
        elif failure == "bad_artifact":
            outcome["artifact"]["status"] = "invalid"
        elif failure == "unapproved":
            value["verdict"] = "changes_requested"
        elif failure == "missing_dimension":
            del assessment["provenance"]
        else:
            raise AssertionError(failure)
        run["outcome_json"] = json.dumps(outcome)
    gate.check(REPO, "main", original, POLICY, github, "token", [entry])
    assert publications[-1][2]["conclusion"] == "failure"
    assert any(action == "dependency_gate_refused" for action, _ in audits)
    unavailable = failure in {
        "malformed_graph",
        "empty_graph",
        "truncated_graph",
        "truncated_files",
        "malformed_files",
        "malformed_alert",
        "truncated_alerts",
        "truncated_compare",
        "wrong_merge_base",
    }
    assert (
        any(action == "dependency_approval_invalidated" for action, _ in audits)
        is not unavailable
    )


@pytest.mark.parametrize(
    "kind,ref,policy",
    [
        ("User", "renovate/locks", POLICY),
        ("User", "dependabot/locks", POLICY),
        ("Bot", "ordinary", POLICY),
        ("User", "ordinary", {**POLICY, "intake": {"dependency_pr_author_ids": [123]}}),
    ],
)
def test_dependency_identity_never_grants_evidence(
    setup, monkeypatch, kind, ref, policy
):
    github, _, _, publications = setup
    github.pull = pull(kind=kind, ref=ref)

    def missing(*_args):
        raise ValueError("missing approved receipt")

    monkeypatch.setattr(gate, "approval", missing)
    gate.check(REPO, "main", github.pull, policy, github, "token")
    assert publications[-1][2]["conclusion"] == "failure"


def test_queue_move_after_evidence_collection_refuses(setup, monkeypatch):
    github, _, _, publications = setup
    monkeypatch.setattr(
        github,
        "queue",
        lambda *_args: [{**queue_entry(), "headCommit": {"oid": "f" * 40}}],
    )
    gate.check(REPO, "main", github.pull, POLICY, github, "token", [queue_entry()])
    assert publications[-1][2]["conclusion"] == "failure"


def test_combined_group_with_dependency_is_refused(setup):
    github, _, _, publications = setup
    gate.check(
        REPO,
        "main",
        github.pull,
        POLICY,
        github,
        "token",
        [queue_entry(), queue_entry(92, 2)],
    )
    assert publications[-1][2]["conclusion"] == "failure"


def test_combined_human_group_passes(setup):
    github, _, _, publications = setup
    github.pull = pull(kind="User", ref="human-fix")
    github.entries = [queue_entry(), queue_entry(91, 2)]
    gate.check(REPO, "main", github.pull, POLICY, github, "token", github.entries)
    assert publications[-1][2]["conclusion"] == "success"


def test_discovery_failure_revokes_known_heads(setup):
    github, _, _, publications = setup

    def unavailable(*_args):
        raise ValueError("queue unavailable")

    github.queue = unavailable
    assert gate.tick(POLICY)["action"] == "refused"
    assert publications[-1][2]["head_sha"] == HEAD
    assert publications[-1][2]["conclusion"] == "failure"


def test_approval_database_failure_revokes_success(setup, monkeypatch):
    github, _, audits, publications = setup
    assert gate.tick(POLICY)["action"] == "checked"
    assert [item[2]["conclusion"] for item in publications] == ["success", "success"]

    def unavailable(*_args):
        raise SQLAlchemyError("approval table unavailable")

    monkeypatch.setattr(gate, "approval", unavailable)
    assert gate.tick(POLICY)["action"] == "checked"
    assert [item[2]["conclusion"] for item in publications[-2:]] == [
        "failure",
        "failure",
    ]
    assert not any(action == "dependency_approval_invalidated" for action, _ in audits)


def test_audit_failure_does_not_block_revocation(setup, monkeypatch):
    github, _, _, publications = setup
    gate.tick(POLICY)
    github.changes[0]["vulnerabilities"] = [{"severity": "high"}]

    def failing_audit(*_args, **_kwargs):
        raise SQLAlchemyError("audit table unavailable")

    monkeypatch.setattr(gate, "audit", failing_audit)
    assert gate.tick(POLICY)["action"] == "checked"
    assert [item[2]["conclusion"] for item in publications[-2:]] == [
        "failure",
        "failure",
    ]


def test_final_read_move_invalidates_generation(setup, monkeypatch):
    github, approved, audits, publications = setup
    entry = queue_entry()
    original = deepcopy(github.pull)

    def honoring_approval(*_args):
        if any(action == "dependency_approval_invalidated" for action, _ in audits):
            raise ValueError(
                "dependency approval invalidated; fresh authorized generation required"
            )
        return approved

    monkeypatch.setattr(gate, "approval", honoring_approval)
    gate.check(REPO, "main", original, POLICY, github, "token", [entry])
    assert publications[-1][2]["conclusion"] == "success"

    pulls_reads = []
    real_get = github.get

    def moving_final_read(repo, endpoint):
        body = real_get(repo, endpoint)
        if endpoint == "pulls/91":
            pulls_reads.append(endpoint)
            if len(pulls_reads) > 1:
                body["head"]["sha"] = "f" * 40
        return body

    monkeypatch.setattr(github, "get", moving_final_read)
    gate.check(REPO, "main", original, POLICY, github, "token", [entry])
    assert pulls_reads == ["pulls/91", "pulls/91"]
    assert publications[-1][2]["conclusion"] == "failure"
    assert any(action == "dependency_approval_invalidated" for action, _ in audits)

    # Restoring the old head must not restore the same approval generation.
    monkeypatch.setattr(github, "get", real_get)
    gate.check(REPO, "main", original, POLICY, github, "token", [entry])
    assert publications[-1][2]["conclusion"] == "failure"


@pytest.mark.parametrize("invalid", [None, True, "Bot", 0, -1])
def test_malformed_author_fails_closed(setup, invalid):
    github, _, _, publications = setup
    github.pull["user"]["id"] = invalid
    gate.check(REPO, "main", github.pull, POLICY, github, "token")
    assert publications[-1][2]["conclusion"] == "failure"


def test_default_intake_authority_remains_empty():
    assert controls.intake_policy({})["dependency_pr_authors"] == []
    assert controls.intake_policy({})["dependency_pr_author_ids"] == []
    assert not controls.intake_policy({})["enabled"]


@pytest.mark.parametrize("invalid", [[True], [0], [-1], ["123"], "123"])
def test_numeric_policy_ids_are_validated(invalid):
    with pytest.raises(ValueError, match="dependency_pr_author_ids"):
        controls.intake_policy({"intake": {"dependency_pr_author_ids": invalid}})


def test_numeric_ids_are_classification_only():
    policy = controls.intake_policy(
        {"intake": {"dependency_pr_author_ids": [123, 123]}}
    )
    assert policy["dependency_pr_author_ids"] == [123]
    assert policy["dependency_pr_authors"] == []


def test_latest_generation_and_durable_invalidation(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gate.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                FactoryAudit,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                SwarmTask,
                SwarmNodeRun,
            )
        ],
    )
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    github = FakeGitHub()
    evidence = deps.snapshot(
        REPO, github.pull, read_get=github.get, read_list=github.rows
    )
    with Session(engine) as db:
        db.add(FactoryControl(id="factory", actor="test"))
        db.add(SwarmTask(id="task", task_text="review", conductor_model="opus"))
        db.add(
            FactoryReceipt(
                repo=REPO,
                issue_number=91,
                generation=1,
                actor="test",
                title="Dependency review",
                body="Inspect",
                url="https://github.com/owner/repo/pull/91",
                task_class="judgment-analysis",
                state="succeeded",
                task_id="task",
                direction_json=json.dumps({"dependency_review": evidence}),
            )
        )
        db.commit()
    assert gate.approval(REPO, 91).evidence == evidence
    gate.audit("dependency_approval_invalidated", task_id="task", receipt_id=1)
    with pytest.raises(ValueError, match="fresh authorized generation"):
        gate.approval(REPO, 91)
    with Session(engine) as db:
        db.add(
            FactoryReceipt(
                repo=REPO,
                issue_number=91,
                generation=2,
                actor="test",
                title="Fresh review",
                body="Inspect",
                url="https://github.com/owner/repo/pull/91",
                task_class="judgment-analysis",
                state="queued",
            )
        )
        db.commit()
    with pytest.raises(ValueError, match="unsettled"):
        gate.approval(REPO, 91)
    engine.dispose()


@pytest.mark.parametrize(
    "failure", ["errors", "null", "truncated", "bad_position", "bad_sha"]
)
def test_queue_discovery_is_bounded_and_fail_closed(monkeypatch, failure):
    github = gate.GitHub("token")
    connection = {"pageInfo": {"hasNextPage": False}, "nodes": [queue_entry()]}
    body = {"data": {"repository": {"mergeQueue": {"entries": connection}}}}
    if failure == "errors":
        body["errors"] = [{"message": "unavailable"}]
    elif failure == "null":
        body["data"]["repository"]["mergeQueue"] = None
    elif failure == "truncated":
        connection["pageInfo"]["hasNextPage"] = True
    elif failure == "bad_position":
        connection["nodes"][0]["position"] = 2
    else:
        connection["nodes"][0]["headCommit"]["oid"] = "bad"
    monkeypatch.setattr(github, "request", lambda *_args: body)
    with pytest.raises((ValueError, TypeError)):
        github.queue(REPO, "main")


def test_github_reads_only_use_dedicated_token(monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN", "never-use")
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert gate.GitHub("app-token").get(REPO, "pulls/91") == {}
    assert seen == ["Bearer app-token"]


def test_check_inventory_and_publication_use_only_dedicated_token(monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN", "never-use")
    monkeypatch.setenv(gate.APP_ID_ENV, str(APP_ID))
    seen = []

    def handler(request):
        seen.append((request.method, request.headers["Authorization"]))
        if request.method == "GET":
            assert request.url.params["check_name"] == gate.CHECK_NAME
            assert request.url.path == f"/repos/{REPO}/commits/{HEAD}/check-runs"
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        return httpx.Response(201, json={"id": 1})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(gate, "audit", lambda *_args, **_kwargs: None)
    gate.publish(REPO, HEAD, "success", "safe", "app-token")
    assert seen == [("GET", "Bearer app-token"), ("POST", "Bearer app-token")]


def test_bad_publication_response_is_not_a_success(setup, monkeypatch):
    monkeypatch.setattr(gate, "_github_post", lambda *_args: {"id": None})
    with pytest.raises(ValueError, match="response malformed"):
        gate.publish(REPO, HEAD, "success", "safe", "token")


def test_shared_commit_refusal_cannot_be_overwritten_by_human_pr(setup, monkeypatch):
    github, _, _, publications = setup
    bot, human = pull(), pull(kind="User", ref="human-fix", number=92)
    original_rows = github.rows
    monkeypatch.setattr(
        github,
        "rows",
        lambda repo, endpoint: (
            [bot, human]
            if endpoint.startswith("pulls?")
            else original_rows(repo, endpoint)
        ),
    )
    monkeypatch.setattr(
        github, "get", lambda repo, endpoint: bot if endpoint == "pulls/91" else human
    )
    github.entries = []

    def missing(*_args):
        raise ValueError("unapproved")

    monkeypatch.setattr(gate, "approval", missing)
    assert gate.tick(POLICY)["action"] == "checked"
    assert len(publications) == 1
    assert publications[0][2]["conclusion"] == "failure"


def test_conductor_runs_gate_before_disabled_lane_return(monkeypatch):
    from factory.orchestration import factory_conductor, uncertain_tasks

    monkeypatch.setattr(uncertain_tasks, "emit_uncertain_task_snapshot", lambda: None)
    monkeypatch.setattr(
        controls, "status", lambda: {"state": "disabled", "policy": POLICY}
    )
    observed = []
    monkeypatch.setattr(gate, "tick", lambda policy: observed.append(policy))
    monkeypatch.setattr(
        factory_conductor.runtime,
        "init_dbos",
        lambda: pytest.fail("must not start guests"),
    )
    factory_conductor.tick()
    assert observed == [POLICY]
