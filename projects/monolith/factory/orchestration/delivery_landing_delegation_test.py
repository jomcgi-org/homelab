"""Standing delivery authority through real intake, review and landing owners."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from core.jsonio import dumps
from sqlmodel import Session, select

from factory.orchestration import delivery_landing_delegation as delegation
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryControl
from factory.orchestration.models import SwarmNodeRun
from factory.orchestration.owner_pr_landing_delegation_test import (  # noqa: F401
    HEAD,
    complete_node,
    db,
    landing_off_on,
    policy,
    principal,
)

BASE = "b" * 40
PATH = "projects/monolith/chat/summaries.py"
REASON = "Land reviewed private deliveries"


def configure(engine, enabled=True, *, max_landings=5, key=None):
    with Session(engine) as session:
        version = session.get(FactoryControl, "factory").version
    limit = {"max_landings": max_landings} if enabled else {}
    preview = delegation.configure(
        principal(), "preview", enabled, version, REASON, **limit
    )
    args = {
        "request_key": key or f"configuration-{version}",
        "expected_preview_sha256": preview["preview_sha256"],
        **limit,
    }
    result = delegation.configure(
        principal(), "apply", enabled, version, REASON, **args
    )
    return preview, result, args


def grants(engine):
    with Session(engine) as session:
        return session.exec(
            select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
        ).all()


def waits(engine):
    with Session(engine) as session:
        return [
            json.loads(row.detail_json)["reason"]
            for row in session.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == "landing_authorization_wait")
                .order_by(FactoryAudit.id)
            ).all()
        ]


def native_case(db, monkeypatch, *, enable=True, body="Tidy private summaries"):
    """Accepted issue -> real intake -> typed review -> native landing.

    No per-head grant is seeded. Only GitHub transport and the external worker
    response are synthetic. Admission, starts, settlement and landing run.
    """
    from factory import review_publisher
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_intake as intake
    from factory.orchestration import factory_intake_loop as loop
    from factory.orchestration import factory_landing as landing
    from factory.orchestration import factory_refine as refine
    from factory.orchestration import work_items

    for module in (conductor, conductor.graph, work_items):
        monkeypatch.setattr(module, "get_engine", lambda: db)
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "3")
    clock = [datetime.now(UTC) + timedelta(seconds=1)]
    monkeypatch.setattr(controls, "_now", lambda: clock[0])
    monkeypatch.setattr(intake, "_now", lambda: clock[0])
    monkeypatch.setattr(loop, "_now", lambda: clock[0])
    raw = policy()
    raw["intake"].pop("owner_pr_author")
    raw["intake"]["refine_enabled"] = False
    live = controls.validate_policy(
        {
            **raw,
            "max_turns_per_task": 18,
            "allowed_models": ["opus", "luna"],
            "conductor_model": "opus",
            "worker_model": "luna",
            "turn_timeout_seconds": 60,
            "task_timeout_seconds": 3600,
            "max_attempts": 2,
        }
    )
    with controls._locked_session() as (_session, control):
        control.policy_json = dumps(live)
    pull = {
        "number": 100,
        "node_id": "delivery-pr",
        "title": "Tidy private summaries",
        "body": "Closes #1",
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "user": {"login": "factory", "id": 7, "type": "User"},
        "assignees": [],
        "labels": [],
        "head": {"ref": "", "sha": HEAD, "repo": {"full_name": delegation.REPO}},
        "base": {"ref": "main", "sha": BASE, "repo": {"full_name": delegation.REPO}},
        "changed_files": 1,
        "auto_merge": None,
        "html_url": f"https://github.com/{delegation.REPO}/pull/100",
        "created_at": "2026-01-01T00:00:00Z",
    }
    issue = {
        "number": 1,
        "title": "Accepted tidy-up",
        "body": body,
        "state": "open",
        "labels": [{"name": "agent-ready"}, {"name": "bug"}],
        "assignees": [],
        "html_url": f"https://github.com/{delegation.REPO}/issues/1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    changed = {"filename": PATH, "status": "modified", "sha": "c" * 40}
    comparison = {"files": [changed], "merge_base_commit": {"sha": BASE}}
    modes = {}
    checks = {"sha": HEAD, "statuses": [{"context": "pr-checks", "state": "success"}]}

    def tree(sha):
        """Synthesize one directory level per changed path on demand."""
        entries = {}
        for f in comparison["files"]:
            parts = f["filename"].split("/")
            for index, part in enumerate(parts):
                parent = "root" if index == 0 else "/".join(parts[:index])
                if hashlib.sha1(parent.encode()).hexdigest() != sha:
                    continue
                leaf = index == len(parts) - 1
                entries[part] = {
                    "path": part,
                    "type": "blob" if leaf else "tree",
                    "mode": modes.get(f["filename"], "100644") if leaf else "040000",
                    "sha": f["sha"]
                    if leaf
                    else hashlib.sha1(
                        "/".join(parts[: index + 1]).encode()
                    ).hexdigest(),
                }
        return {"truncated": False, "tree": list(entries.values())}

    def github(repo, suffix):
        assert repo == delegation.REPO
        if suffix == "pulls/100":
            return pull
        if suffix == "issues/1":
            return issue
        if suffix.startswith("compare/"):
            return comparison
        if suffix.startswith("git/commits/"):
            return {"tree": {"sha": hashlib.sha1(b"root").hexdigest()}}
        if suffix.startswith("git/trees/"):
            return tree(suffix.split("/")[-1])
        if suffix == f"commits/{HEAD}/status":
            return checks
        pytest.fail(f"Unexpected GitHub read: {suffix}")

    def listing(repo, suffix):
        assert repo == delegation.REPO
        if suffix.startswith("issues?"):
            return [issue]
        if suffix.startswith("pulls?"):
            return []
        if suffix.startswith("issues/") and "/comments" in suffix:
            return []
        pytest.fail(f"Unexpected GitHub list: {suffix}")

    for module in (conductor, loop, refine):
        monkeypatch.setattr(module, "github_get", github, raising=False)
        monkeypatch.setattr(module, "github_list", listing)
    if enable:
        configure(db)
    assert len(loop.intake_tick(live, generation=1)) == 1
    admitted = intake.admit_next("factory:conductor")
    assert admitted["ok"], admitted
    task = conductor._task(admitted["task_id"])
    assert conductor.delivery_branch(task) == f"factory/{task['id']}"
    pull["head"]["ref"] = f"factory/{task['id']}"
    complete_node(task, live, "implement_fix", {"pr_number": 100, "head_sha": HEAD})
    scope = {
        "classification": "private_internal_reversible",
        "public_facing": False,
        "security_sensitive": False,
        "authentication_or_credentials": False,
        "destructive": False,
        "evidence": "Private summary formatting only, with no authority or storage change.",
        "base_sha": BASE,
        "paths": [PATH],
    }
    review = complete_node(
        task,
        live,
        "review_fix",
        {
            "verdict": "approve",
            "summary": "Independent exact-head review",
            "pr_number": 100,
            "head_sha": HEAD,
            "landing_scope": scope,
        },
        deps=["implement_fix"],
    )
    result = controls.finish_task(
        task["id"],
        "succeeded",
        "factory:conductor",
        evidence={
            "pr_url": pull["html_url"],
            "head_sha": HEAD,
            "review_session_id": review["session_id"],
            "state": "ready_for_review",
        },
    )
    assert result["state"] == "landing", result
    assert isinstance(
        review_publisher.collect(task["id"]), review_publisher.ReviewEvidence
    )
    mutations, queue = [], set()

    def graphql(query, args):
        if query == landing._QUEUE_ENTRIES:
            return {
                "nodes": [
                    {
                        "id": node,
                        "mergeQueueEntry": {"id": "entry"} if node in queue else None,
                    }
                    for node in args["ids"]
                ]
            }
        mutations.append((query, args))
        if query == landing._ARM_AUTO_MERGE:
            pull["auto_merge"] = {"enabledBy": {"login": "factory"}}
            queue.add("delivery-pr")
        elif query == landing._DISARM_AUTO_MERGE:
            pull["auto_merge"] = None
        elif query == landing._DEQUEUE_PULL_REQUEST:
            queue.discard("delivery-pr")
        else:
            pytest.fail("Unexpected GitHub mutation")
        return {}

    monkeypatch.setattr(landing, "github_graphql", graphql)
    return SimpleNamespace(
        task=task,
        live=live,
        pull=pull,
        issue=issue,
        comparison=comparison,
        changed=changed,
        modes=modes,
        checks=checks,
        review=review,
        scope=scope,
        mutations=mutations,
        queue=queue,
        clock=clock,
    )


def rewrite_scope(engine, case, **changes):
    with controls._locked_session() as (session, _):
        run = session.get(SwarmNodeRun, case.review["id"])
        outcome = json.loads(run.outcome_json)
        for holder in (outcome["artifact"]["value"], outcome["value"]):
            holder["landing_scope"] = {**holder["landing_scope"], **changes}
        run.outcome_json = dumps(outcome)


@pytest.mark.parametrize(
    "path",
    [
        "projects/monolith/chat/summaries.py",
        "projects/embervm/runtimes/ping/main.go",
        "projects/embervm/runtimes/ping/BUILD",
        "projects/scotland-connector/internal/forecast/window_test.go",
        "projects/monolith/frontend/src/lib/server/imgproxy.js",
    ],
)
def test_private_project_paths_are_inside_the_boundary(path):
    assert delegation.denied(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "docs/agents/evolution.md",
        "bazel/tools/ci/main.go",
        "buildbuddy.yaml",
        ".github/workflows/ci.yaml",
        "projects/monolith/AGENTS.md",
        "projects/monolith/factory/orchestration/factory_landing.py",
        "projects/monolith/factory/review_publisher.py",
        "projects/monolith/chart/migrations/20261109000000_add.sql",
        "projects/monolith/chart/values.yaml",
        "projects/embervm/deploy/values-gke.yaml",
        "projects/embervm/chart/templates/noded.yaml",
        "projects/gke-apps/monolith/application.yaml",
        "projects/platform/authentik/blueprint.yaml",
        "projects/monolith/auth/api.py",
        "projects/embervm/tokenbroker/main.go",
        "projects/monolith/shared/oauth_client.py",
        "projects/monolith/chat/secret_store.py",
        "projects/monolith/frontend/src/routes/public/slop/+page.svelte",
        "projects/monolith-public/chart/values.yaml",
        "projects/monolith/chat_public/api.py",
        "projects/monolith/bdd_test.bzl",
        "projects/monolith/.hidden/config.py",
        "projects/monolith/../../AGENTS.md",
        "projects//monolith/chat.py",
        " projects/monolith/chat.py",
        None,
    ],
)
def test_authority_deployment_credential_and_public_paths_need_an_operator(path):
    assert delegation.denied(path) is True


def test_standing_preview_apply_is_default_off_bounded_and_replay_safe(db):
    with Session(db) as session:
        assert delegation.status(session) == {
            "enabled": False,
            "reason": "delivery_landing_disabled",
        }
        before = session.get(FactoryControl, "factory").policy_json
    preview, applied, args = configure(db)
    assert preview["deny_rules"] == delegation.DENY_RULES
    assert (preview["max_landings"], preview["max_files"]) == (5, 40)
    assert applied == {
        "ok": True,
        "enabled": True,
        "version": applied["version"],
        "dispatched": False,
    }
    version = applied["version"] - 1
    assert (
        delegation.configure(principal(), "apply", True, version, REASON, **args)
        == applied
    )
    with pytest.raises(ValueError, match="delivery_landing_request_conflict"):
        delegation.configure(
            principal(), "apply", True, version, REASON, **{**args, "max_landings": 6}
        )
    with Session(db) as session:
        assert session.get(FactoryControl, "factory").policy_json == before
        status = delegation.status(session)
    assert status["enabled"] is True
    assert (status["derivations_used"], status["derivations_remaining"]) == (0, 5)
    configure(db, False)
    with Session(db) as session:
        assert delegation.status(session)["reason"] == "delivery_landing_disabled"


@pytest.mark.parametrize(
    ("enabled", "limit", "reason"),
    [
        (True, None, "limit_invalid"),
        (True, 0, "limit_invalid"),
        (True, 51, "limit_invalid"),
        (True, True, "limit_invalid"),
        (False, 5, "limit_invalid"),
    ],
)
def test_configuration_refuses_unbounded_requests(db, enabled, limit, reason):
    with pytest.raises(ValueError, match="delivery_landing_" + reason):
        delegation.configure(
            principal(), "preview", enabled, 0, REASON, max_landings=limit
        )


def test_configuration_requires_a_standing_operator_and_current_preview(db):
    from dataclasses import replace

    with pytest.raises(ValueError, match="standing_human_required"):
        delegation.configure(
            replace(principal(), groups=()), "preview", True, 0, REASON, max_landings=5
        )
    with Session(db) as session:
        version = session.get(FactoryControl, "factory").version
    preview = delegation.configure(
        principal(), "preview", True, version, REASON, max_landings=5
    )
    with pytest.raises(ValueError, match="delivery_landing_preview_changed"):
        delegation.configure(
            principal(),
            "apply",
            True,
            version,
            REASON,
            max_landings=6,
            request_key="widened",
            expected_preview_sha256=preview["preview_sha256"],
        )
    with pytest.raises(ValueError, match="delivery_landing_version_changed"):
        delegation.configure(
            principal(), "preview", True, version + 1, REASON, max_landings=5
        )


def test_native_delivery_lands_without_per_head_grant(db, monkeypatch):
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch)
    landing.landing_tick(case.live)
    assert case.queue == {"delivery-pr"}
    assert case.mutations == [
        (
            landing._ARM_AUTO_MERGE,
            {"pullRequestId": "delivery-pr", "expectedHeadOid": HEAD},
        )
    ]
    events = grants(db)
    assert len(events) == 1
    assert events[0].actor == delegation.ACTOR
    detail = json.loads(events[0].detail_json)
    assert detail["scope"] == case.scope
    assert detail["files"]["files"] == [{"path": PATH, "head_blob_sha": "c" * 40}]
    with Session(db) as session:
        row = controls._receipt(session, case.task["id"])
        assert "landing_authorization" not in json.loads(row.direction_json or "{}")
        assert delegation.status(session)["derivations_used"] == 1
    landing.landing_tick(case.live)
    assert len(grants(db)) == 1


def test_native_delivery_lands_added_removed_and_renamed_files(db, monkeypatch):
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch)
    files = [
        {
            "filename": "projects/monolith/chat/BUILD",
            "status": "modified",
            "sha": "1" * 40,
        },
        {
            "filename": "projects/monolith/chat/digest.py",
            "status": "added",
            "sha": "2" * 40,
        },
        {
            "filename": "projects/monolith/chat/legacy.py",
            "status": "removed",
            "sha": "3" * 40,
        },
        {
            "filename": "projects/monolith/chat/render.py",
            "previous_filename": "projects/monolith/chat/renderer.py",
            "status": "renamed",
            "sha": "4" * 40,
        },
    ]
    case.comparison["files"] = files
    case.pull["changed_files"] = len(files)
    rewrite_scope(db, case, paths=[f["filename"] for f in files])
    landing.landing_tick(case.live)
    assert case.queue == {"delivery-pr"}
    detail = json.loads(grants(db)[0].detail_json)
    assert [f["head_blob_sha"] for f in detail["files"]["files"]] == [
        "1" * 40,
        "2" * 40,
        None,
        "4" * 40,
    ]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("disabled", "delivery_landing_disabled"),
        ("revoke", "delivery_landing_disabled"),
        ("landing_off_on", "delivery_landing_policy_replaced"),
        ("rules", "delivery_landing_bounds_changed"),
        ("path", "delivery_landing_paths_require_operator"),
        ("factory_path", "delivery_landing_paths_require_operator"),
        ("renamed_from_denied", "delivery_landing_paths_require_operator"),
        ("mode", "delivery_landing_file_type_requires_operator"),
        ("blob", "delivery_landing_file_type_requires_operator"),
        ("incomplete", "delivery_landing_files_incomplete"),
        ("too_many", "delivery_landing_file_limit"),
        ("scope_paths", "delivery_landing_review_scope_changed"),
        ("merge_base", "delivery_landing_review_scope_changed"),
        ("public", "delivery_landing_scope_requires_operator"),
        ("security", "delivery_landing_scope_requires_operator"),
        ("credentials", "delivery_landing_scope_requires_operator"),
        ("destructive", "delivery_landing_scope_requires_operator"),
        ("classification", "delivery_landing_scope_requires_operator"),
        ("scope_missing", "delivery_landing_scope_missing_or_invalid"),
        # The review collector binds branch and base before this owner does.
        ("branch", "delivery_landing_independent_review_missing"),
        ("base", "delivery_landing_independent_review_missing"),
        ("ci", "operator_handoff_ci_unverified"),
        ("issue_closed", "delivery_landing_issue_held"),
        ("issue_assigned", "delivery_landing_issue_held"),
        # Landing's own human-ownership observer stops these before any grant.
        ("issue_human", None),
        ("unknown", None),
    ],
)
def test_native_landing_waits_outside_the_standing_boundary(
    db, monkeypatch, change, reason
):
    import httpx

    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch, enable=change != "disabled")
    if change == "revoke":
        configure(db, False)
    elif change == "landing_off_on":
        landing_off_on(db)
    elif change == "rules":
        monkeypatch.setattr(
            delegation,
            "DENY_RULES",
            {**delegation.DENY_RULES, "suffixes": [".bzl", ".sh"]},
        )
    elif change == "path":
        case.changed["filename"] = "projects/monolith/auth/api.py"
        rewrite_scope(db, case, paths=["projects/monolith/auth/api.py"])
    elif change == "factory_path":
        target = "projects/monolith/factory/orchestration/factory_landing.py"
        case.changed["filename"] = target
        rewrite_scope(db, case, paths=[target])
    elif change == "renamed_from_denied":
        case.changed.update(
            status="renamed", previous_filename="projects/monolith/auth/summaries.py"
        )
    elif change == "mode":
        case.modes[PATH] = "100755"
    elif change == "blob":
        real = conductor.github_get

        def stale_tree(repo, suffix):
            result = real(repo, suffix)
            if suffix.startswith("git/trees/"):
                for entry in result["tree"]:
                    if entry["type"] == "blob":
                        entry["sha"] = "e" * 40
            return result

        monkeypatch.setattr(conductor, "github_get", stale_tree)
    elif change == "incomplete":
        case.pull["changed_files"] = 2
    elif change == "too_many":
        case.pull["changed_files"] = delegation.MAX_FILES + 1
    elif change == "scope_paths":
        rewrite_scope(db, case, paths=[PATH, "projects/monolith/chat/other.py"])
    elif change == "merge_base":
        case.comparison["merge_base_commit"]["sha"] = "d" * 40
    elif change in {"public", "security", "credentials", "destructive"}:
        key = {
            "public": "public_facing",
            "security": "security_sensitive",
            "credentials": "authentication_or_credentials",
            "destructive": "destructive",
        }[change]
        rewrite_scope(db, case, **{key: True})
    elif change == "classification":
        rewrite_scope(db, case, classification="requires_operator")
    elif change == "scope_missing":
        rewrite_scope(db, case, evidence="short")
    elif change == "branch":
        case.pull["head"]["ref"] = "fix/someone-elses-branch"
    elif change == "base":
        case.pull["base"]["ref"] = "release"
    elif change == "ci":
        case.checks["statuses"][0]["state"] = "failure"
    elif change == "issue_closed":
        case.issue["state"] = "closed"
    elif change == "issue_assigned":
        case.issue["assignees"] = [{"login": "jomcgi"}]
    elif change == "issue_human":
        case.issue["labels"].append({"name": "human"})
    elif change == "unknown":
        read = conductor.github_get

        def uncertain(repo, suffix):
            if suffix == "issues/1":
                raise httpx.ReadTimeout("uncertain objective")
            return read(repo, suffix)

        monkeypatch.setattr(conductor, "github_get", uncertain)
    landing.landing_tick(case.live)
    assert not case.queue
    assert case.pull["auto_merge"] is None
    assert not case.mutations
    assert not grants(db)
    assert (waits(db) or [None])[-1] == reason


def test_rolling_and_task_limits_wait_without_granting(db, monkeypatch):
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch, enable=False)
    configure(db, max_landings=1)
    with controls._locked_session() as (session, _):
        controls._audit(
            session, delegation.ACTOR, delegation.GRANT, task_id="task-5", other=True
        )
    landing.landing_tick(case.live)
    assert not case.queue
    assert waits(db)[-1] == "delivery_landing_rolling_limit"
    # The window, not reconfiguration, releases the cap.
    case.clock[0] += delegation.RATE_WINDOW + timedelta(minutes=1)
    with controls._locked_session() as (session, _):
        for index in range(delegation.MAX_TASK_DERIVATIONS):
            controls._audit(
                session,
                delegation.ACTOR,
                delegation.GRANT,
                task_id=case.task["id"],
                stale=index,
            )
    case.clock[0] += delegation.RATE_WINDOW + timedelta(minutes=1)
    assert (
        delegation.prepare(case.task["id"], 100, HEAD) == "delivery_landing_task_limit"
    )


def test_revocation_fences_a_queued_delivery(db, monkeypatch):
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch)
    landing.landing_tick(case.live)
    assert case.queue == {"delivery-pr"}
    configure(db, False)
    landing.landing_tick(case.live)
    assert not case.queue
    assert case.pull["auto_merge"] is None


@pytest.mark.parametrize(
    ("direction", "body", "owner"),
    [
        ({}, "ordinary", "delivery_landing_delegation"),
        ({"owner_pr": {"number": 1}}, "ordinary", "owner_pr_landing_delegation"),
        (
            {},
            "<!-- factory-problem:" + "a" * 64 + " -->",
            "repair_landing_delegation",
        ),
        ({"landing_authorization": {}}, "ordinary", None),
    ],
)
def test_each_receipt_has_exactly_one_authority_owner(direction, body, owner):
    from factory.orchestration import factory_landing_release as releases

    row = SimpleNamespace(direction_json=dumps(direction), body=body)
    selected = releases.delegation_owner(row)
    assert (selected.__name__.rsplit(".", 1)[-1] if selected else None) == owner


def test_repair_and_owner_receipts_never_use_delivery_authority(db, monkeypatch):
    case = native_case(db, monkeypatch)
    for change in ("repair", "owner_pr", "human_grant"):
        with controls._locked_session() as (session, _):
            row = controls._receipt(session, case.task["id"])
            direction = json.loads(row.direction_json or "{}")
            direction.pop("owner_pr", None)
            row.body = "ordinary"
            if change == "repair":
                row.body = "<!-- factory-problem:" + "a" * 64 + " -->"
            elif change == "owner_pr":
                direction["owner_pr"] = {"number": 100}
            else:
                direction["landing_authorization"] = None
            row.direction_json = dumps(direction)
        assert (
            delegation.prepare(case.task["id"], 100, HEAD)
            == "delivery_landing_other_owner"
        )
    assert not grants(db)


@pytest.mark.parametrize("change", ["branch", "base", "draft", "merged", "fork"])
def test_only_the_tasks_own_open_factory_branch_qualifies(db, monkeypatch, change):
    from factory import review_publisher

    case = native_case(db, monkeypatch)
    evidence = review_publisher.collect(case.task["id"])
    # Hold the collector's own identity binding constant to reach this owner's.
    monkeypatch.setattr(review_publisher, "collect", lambda *_a, **_k: evidence)
    if change == "branch":
        case.pull["head"]["ref"] = "fix/someone-elses-branch"
    elif change == "base":
        case.pull["base"]["ref"] = "release"
    elif change == "fork":
        case.pull["head"]["repo"]["full_name"] = "someone/homelab"
    else:
        case.pull[change] = True
    assert (
        delegation.prepare(case.task["id"], 100, HEAD)
        == "delivery_landing_pull_identity_changed"
    )
    assert not grants(db)
