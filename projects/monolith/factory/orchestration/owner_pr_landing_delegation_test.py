"""Standing owner authority and real intake, review and landing integration."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from auth.api import Authority, Principal, PrincipalKind
from core.jsonio import dumps
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls
from factory.orchestration import owner_pr_landing_delegation as delegation
from factory.orchestration import owner_prs
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
OWNER = {"login": "jomcgi", "id": 116890464}
HEAD = "a" * 40


def principal():
    return Principal(
        subject="operator",
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )


def policy():
    return {
        "repo": delegation.REPO,
        "base_branch": "main",
        "generation": 1,
        "auto_merge": True,
        "issue_numbers": [],
        "task_budget_usd": 100,
        "turn_budget_usd": 10,
        "max_tasks": {"delivery": 2, "advisory": 1},
        "intake": {
            "enabled": True,
            "refine_enabled": True,
            "owner_pr_author": OWNER,
            "exclude_labels": [],
        },
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'owner-landing.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                SwarmConductorCall,
                SwarmNodeRun,
                SwarmPlanNode,
                SwarmPlanVersion,
                FactoryControl,
                FactoryReceipt,
                FactoryStart,
                FactoryAudit,
                FactoryClassTier,
                FactoryReviewVerdict,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                AgentSession,
                PendingMessage,
            )
        ],
    )
    with Session(engine) as session:
        session.add(
            FactoryControl(
                id="factory",
                actor="operator",
                state="enabled",
                policy_json=dumps(policy()),
            )
        )
        session.add_all(
            [
                SwarmTask(
                    id=f"task-{n}",
                    task_text="work",
                    conductor_model="astra",
                    repo=delegation.REPO,
                    base_branch="main",
                    budget_usd=100,
                )
                for n in range(1, 6)
            ]
        )
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: NOW)
    yield engine
    engine.dispose()


def complete_node(task, live, key, value, *, deps=()):
    """Model a dispatched worker response through the real graph/start owners."""
    from factory.orchestration import factory_conductor as conductor

    assert conductor._add(
        task,
        live,
        key,
        "bounded work",
        list(deps),
        "opus" if key.startswith("review_") else "luna",
        f"test:{key}",
        "native fixture",
        review=key.startswith("review_"),
    ).ok
    workflow = f"factory-node:{task['id']}:{key}:1"
    context = {
        "repo": task["repo"],
        "branch": conductor.delivery_branch(task),
        "workflow_id": workflow,
        "artifact_path": f".factory/{key}.json",
        "artifact_schema": conductor._schema(key),
        "hydration_branch": "main",
        "retry_context": "[]",
    }
    assert conductor.reserve_node(task["id"], key, workflow, context)
    session_id = 100 + len(controls.task_snapshot(task["id"])["starts"])
    outcome = {
        "status": "succeeded",
        "session_id": session_id,
        "cost_usd": 0.25,
        "head_sha": HEAD,
        "value": value,
        "artifact": {"status": "ok", "value": value, "errors": []},
        "cleanup": {"status": "completed"},
    }
    assert conductor.graph.record_dispatch(task["id"], key, 1, session_id, None).ok
    assert conductor.graph.record_outcome(
        task["id"], key, 1, "succeeded", 0.25, HEAD, dumps(outcome)
    ).ok
    assert controls.record_start_outcome(
        task["id"],
        workflow,
        "succeeded",
        "worker",
        cost_usd=0.25,
        session_id=session_id,
    )["ok"]
    return next(
        r for r in conductor.graph.node_runs(task["id"]) if r["node_key"] == key
    )


def native_case(db, monkeypatch, *, direct=False, exclude_labels=()):
    """Existing PR -> real intake/refinement -> typed review -> native landing.

    No per-head grant is seeded. Only GitHub transport and the external worker
    response are synthetic. Admission, starts, settlement and publication run.
    """
    import base64
    import hashlib

    from factory import review_publisher
    from factory.orchestration import (
        factory_conductor as conductor,
    )
    from factory.orchestration import (
        factory_intake as intake,
    )
    from factory.orchestration import (
        factory_intake_loop as loop,
    )
    from factory.orchestration import (
        factory_landing as landing,
    )
    from factory.orchestration import (
        factory_refine as refine,
    )
    from factory.orchestration import (
        work_items,
    )

    for module in (conductor, conductor.graph, work_items):
        monkeypatch.setattr(module, "get_engine", lambda: db)
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "3")
    clock = [datetime.now(UTC) + timedelta(seconds=1)]
    monkeypatch.setattr(controls, "_now", lambda: clock[0])
    monkeypatch.setattr(intake, "_now", lambda: clock[0])
    monkeypatch.setattr(loop, "_now", lambda: clock[0])
    live = controls.validate_policy(
        {
            **policy(),
            "max_turns_per_task": 18,
            "allowed_models": ["opus", "luna"],
            "conductor_model": "opus",
            "worker_model": "luna",
            "turn_timeout_seconds": 60,
            "task_timeout_seconds": 3600,
            "max_attempts": 2,
        }
    )
    live["intake"]["exclude_labels"] = list(exclude_labels)
    with controls._locked_session() as (_session, control):
        control.policy_json = dumps(live)
    path = "projects/monolith/factory/orchestration/factory_feedback.py"
    base = "b" * 40
    pull = {
        "number": 100,
        "node_id": "owner-pr",
        "title": "Repair private diagnostics",
        "body": "Private diagnostics maintenance" if direct else "Closes #1",
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "user": {**OWNER, "type": "User"},
        "assignees": [],
        "labels": [],
        "head": {
            "ref": "fix/preexisting",
            "sha": HEAD,
            "repo": {"full_name": delegation.REPO},
        },
        "base": {"ref": "main", "sha": base, "repo": {"full_name": delegation.REPO}},
        "changed_files": 1,
        "additions": 1,
        "deletions": 1,
        "auto_merge": None,
        "html_url": f"https://github.com/{delegation.REPO}/pull/100",
        "created_at": "2026-01-01T00:00:00Z",
    }
    issue = {
        "number": 1,
        "title": "Accepted fix",
        "body": "Repair private diagnostics",
        "state": "open",
        "labels": [{"name": "agent-ready"}, {"name": "bug"}],
        "assignees": [],
        "html_url": f"https://github.com/{delegation.REPO}/issues/1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    issues = {100: pull} if direct else {1: issue, 100: pull}
    comments = {number: [] for number in issues}
    changed = {"filename": path, "status": "modified", "sha": "c" * 40}
    comparison = {"files": [changed], "merge_base_commit": {"sha": base}}
    trees = {}
    for revision in (base, HEAD):
        tree = revision
        for index, part in enumerate(path.split("/")):
            child = hashlib.sha1(f"{revision}/{index}".encode()).hexdigest()
            leaf = index == len(path.split("/")) - 1
            trees[tree] = {
                "truncated": False,
                "tree": [
                    {
                        "path": part,
                        "type": "blob" if leaf else "tree",
                        "mode": "100644" if leaf else "040000",
                        "sha": ("c" if revision == HEAD else "d") * 40
                        if leaf
                        else child,
                    }
                ],
            }
            tree = child
    checks = {"sha": HEAD, "statuses": [{"context": "pr-checks", "state": "success"}]}

    def github(repo, suffix):
        assert repo == delegation.REPO
        if suffix == "pulls/100":
            return pull
        if suffix.startswith("issues/") and suffix.split("/")[-1].isdigit():
            return issues[int(suffix.split("/")[-1])]
        if suffix.startswith("compare/"):
            return comparison
        if suffix.startswith("git/commits/"):
            return {"tree": {"sha": suffix.split("/")[-1]}}
        if suffix.startswith("git/trees/"):
            return trees[suffix.split("/")[-1]]
        if suffix == "git/blobs/" + "c" * 40:
            return {
                "encoding": "base64",
                "size": 5,
                "content": base64.b64encode(b"pass\n").decode(),
            }
        if suffix == f"commits/{HEAD}/status":
            return checks
        pytest.fail(f"Unexpected GitHub read: {suffix}")

    def listing(repo, suffix):
        assert repo == delegation.REPO
        if suffix.startswith("issues?"):
            return [issue] if not direct else []
        if suffix.startswith("pulls?"):
            return [pull]
        if suffix.startswith("issues/") and "/comments" in suffix:
            return comments[int(suffix.split("/")[1])]
        pytest.fail(f"Unexpected GitHub list: {suffix}")

    for module in (conductor, loop, refine):
        monkeypatch.setattr(module, "github_get", github, raising=False)
        monkeypatch.setattr(module, "github_list", listing)
    # The PR exists before standing authorization, as the preview explicitly permits.
    configure(db)
    assert len(loop.intake_tick(live, generation=1)) == 1
    admitted = intake.admit_next("factory:conductor")
    assert admitted["ok"], admitted
    task = conductor._task(admitted["task_id"])
    if direct:
        assert controls.task_snapshot(task["id"])["task_class"] == "refine"
        comment = {
            "body": "## Agent brief\n### Outcome\nRepair private diagnostics using the existing PR.",
            "html_url": f"https://github.com/{delegation.REPO}/pull/100#issuecomment-1",
            "created_at": clock[0].isoformat(),
            "user": {"login": "factory"},
        }
        comments[100].append(comment)
        pull["labels"] = [{"name": "agent-ready"}, {"name": "bug"}]
        run = complete_node(
            task,
            live,
            "refine_1",
            {
                "outcome": "agent-ready",
                "comment_url": comment["html_url"],
            },
        )
        refine._settle(task, run, live)
        assert controls.task_snapshot(task["id"])["state"] == "succeeded"
        clock[0] += timedelta(minutes=5)
        assert len(loop.intake_tick(live, generation=1)) == 1
        admitted = intake.admit_next("factory:conductor")
        assert admitted["ok"], admitted
        task = conductor._task(admitted["task_id"])
    assert task["issue_number"] == (100 if direct else 1)
    assert conductor.delivery_branch(task) == "fix/preexisting"
    complete_node(task, live, "implement_fix", {"pr_number": 100, "head_sha": HEAD})
    scope = {
        "classification": "private_internal_reversible",
        "public_facing": False,
        "security_sensitive": False,
        "authentication_or_credentials": False,
        "destructive": False,
        "evidence": "Private observation formatting only, with no authority or storage change.",
        "base_sha": base,
        "paths": [path],
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
    evidence = review_publisher.collect(task["id"])
    assert isinstance(evidence, review_publisher.ReviewEvidence), evidence
    published = []
    monkeypatch.setenv(review_publisher.PUBLISH_ENABLED_ENV, "true")
    monkeypatch.setenv(review_publisher.PUBLISHER_TOKEN_ENV, "synthetic-test-token")

    def publish(repo, suffix, body, _token):
        assert (repo, suffix) == (delegation.REPO, "check-runs")
        published.append(body)
        return {"id": 7}

    monkeypatch.setattr(review_publisher, "_github_post", publish)
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
            queue.add("owner-pr")
        elif query == landing._DISARM_AUTO_MERGE:
            pull["auto_merge"] = None
        elif query == landing._DEQUEUE_PULL_REQUEST:
            queue.discard("owner-pr")
        else:
            pytest.fail("Unexpected GitHub mutation")
        return {}

    monkeypatch.setattr(landing, "github_graphql", graphql)
    return SimpleNamespace(
        task=task,
        live=live,
        pull=pull,
        issue=issue,
        issues=issues,
        comments=comments,
        comparison=comparison,
        changed=changed,
        trees=trees,
        checks=checks,
        review=review,
        mutations=mutations,
        queue=queue,
        published=published,
    )


@pytest.mark.parametrize(
    "direct", [False, True], ids=["accepted-issue", "settled-pr-refinement"]
)
def test_native_owner_feed_review_and_landing_without_per_head_grant(
    db, monkeypatch, direct
):
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch, direct=direct)
    landing.landing_tick(case.live)
    assert case.queue == {"owner-pr"}
    assert case.published[0]["conclusion"] == "success"
    assert case.published[0]["head_sha"] == HEAD
    assert case.mutations == [
        (
            landing._ARM_AUTO_MERGE,
            {"pullRequestId": "owner-pr", "expectedHeadOid": HEAD},
        )
    ]
    with Session(db) as session:
        events = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
        ).all()
        assert len(events) == 1
        assert events[0].actor == delegation.ACTOR
        row = controls._receipt(session, case.task["id"])
        assert "landing_authorization" not in json.loads(row.direction_json)
    landing.landing_tick(case.live)
    with Session(db) as session:
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        "pr_hold",
        "reference_hold",
        "new_reference_hold",
        "local_hold",
        "owner",
        "branch",
        "base",
        "head",
        "review",
        "ci",
        "path",
        "mode",
        "unknown",
        "claimed",
        "revoke",
        "restore",
        "human_grant",
        "excluded_pr_label",
        "historical_pause",
        "historical_cancellation",
        "landing_off_on",
    ],
)
def test_native_landing_fences_fresh_authority_changes(db, monkeypatch, queued, change):
    import httpx

    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch, exclude_labels=["bespoke-protected"])
    if queued:
        landing.landing_tick(case.live)
        assert case.queue == {"owner-pr"}
    if change == "pr_hold":
        case.comments[100].append({"body": "Do not merge this PR"})
    elif change == "excluded_pr_label":
        case.pull["labels"] = [{"name": "bespoke-protected"}]
    elif change == "reference_hold":
        case.comments[1].append({"body": "Leave unmerged until I approve"})
    elif change == "new_reference_hold":
        case.pull["body"] += "\nRefs #2"
        case.issues[2] = {
            **case.issue,
            "number": 2,
            "state": "closed",
            "labels": [{"name": "human"}],
        }
        case.comments[2] = []
    elif change == "owner":
        case.pull["user"]["id"] += 1
    elif change in {"branch", "base", "head"}:
        target = case.pull["base"] if change == "base" else case.pull["head"]
        target["sha" if change == "head" else "ref"] = (
            "e" * 40 if change == "head" else "other"
        )
    elif change == "ci":
        case.checks["statuses"][0]["state"] = "failure"
    elif change == "path":
        case.changed["filename"] = "projects/monolith/auth/api.py"
    elif change == "mode":
        for tree in case.trees.values():
            if tree["tree"][0]["type"] == "blob":
                tree["tree"][0]["mode"] = "100755"
    elif change == "unknown":
        read = conductor.github_get

        def uncertain(repo, suffix):
            if suffix == "issues/1":
                raise httpx.ReadTimeout("uncertain objective")
            return read(repo, suffix)

        monkeypatch.setattr(conductor, "github_get", uncertain)
    elif change in {"revoke", "restore"}:
        configure(db, False)
        if change == "restore":
            configure(db)
    elif change == "landing_off_on":
        landing_off_on(db)
    else:
        with controls._locked_session() as (session, _):
            row = controls._receipt(session, case.task["id"])
            if change == "local_hold":
                session.get(WorkItem, row.work_item_id).authority = "local"
            elif change == "review":
                run = session.get(SwarmNodeRun, case.review["id"])
                outcome = json.loads(run.outcome_json)
                outcome["artifact"]["value"]["head_sha"] = "e" * 40
                outcome["value"]["head_sha"] = "e" * 40
                run.outcome_json = dumps(outcome)
            elif change == "human_grant":
                direction = json.loads(row.direction_json)
                direction["landing_authorization"] = None
                row.direction_json = dumps(direction)
            elif change == "claimed":
                agent = AgentSession(
                    local_session_id=f"factory:{row.task_id}:native",
                    status="completed",
                    workspace="/workspace/src",
                    branch="fix/preexisting",
                )
                session.add(agent)
                session.flush()
                session.add(
                    PendingMessage(
                        session_id=agent.id,
                        seq=2,
                        message_text="still claimed",
                        claimed_by_replica="live-owner",
                        claimed_at=NOW - timedelta(days=2),
                    )
                )
            elif change in {"historical_pause", "historical_cancellation"}:
                session.add(
                    FactoryReceipt(
                        repo=delegation.REPO,
                        issue_number=1,
                        generation=0,
                        work_item_id=row.work_item_id,
                        title="Prior attempt",
                        body="",
                        url=case.issue["html_url"],
                        actor=controls.INTAKE_ACTOR,
                        task_id="task-2",
                        task_class="bug-fix",
                        state="failed",
                        task_paused=change == "historical_pause",
                        cancellation_requested=change == "historical_cancellation",
                    )
                )
    landing.landing_tick(case.live)
    fresh_grant = change == "restore" and not queued
    assert bool(case.queue) is fresh_grant
    assert (case.pull["auto_merge"] is not None) is fresh_grant
    arms = [query for query, _ in case.mutations if query == landing._ARM_AUTO_MERGE]
    assert len(arms) == int(queued or fresh_grant)
    with Session(db) as session:
        grants = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
        ).all()
        # Restoring standing authority fences an already queued obsolete grant.
        # A later fresh tick can derive a new one, spending another slot.
        assert len(grants) == int(queued or fresh_grant)


def test_native_derivation_cap_survives_revocation_and_reauthorization(db, monkeypatch):
    case = native_case(db, monkeypatch)
    for number in range(3):
        if number:
            configure(db, False)
            configure(db)
        assert delegation.prepare(case.task["id"], 100, HEAD) is None
        assert delegation.prepare(case.task["id"], 100, HEAD) is None
    configure(db, False)
    configure(db)
    assert delegation.prepare(case.task["id"], 100, HEAD) == "owner_pr_landing_pr_limit"


def test_native_hold_after_derivation_is_rechecked_before_arm(db, monkeypatch):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_landing as landing

    case = native_case(db, monkeypatch)
    read = conductor.github_get
    observed = []

    def github(repo, suffix):
        if suffix == "pulls/100" and not observed:
            with Session(db) as session:
                derived = controls.latest_audit(session, delegation.GRANT)
            if derived:
                observed.append(True)
                case.comments[1].append({"body": "Do not merge after this review"})
        return read(repo, suffix)

    monkeypatch.setattr(conductor, "github_get", github)
    landing.landing_tick(case.live)
    assert observed and not case.queue and not case.mutations
    with Session(db) as session:
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
                ).all()
            )
            == 1
        )


def configure(db, enabled=True, *, key=None):
    with Session(db) as session:
        version = session.get(FactoryControl, "factory").version
    preview = delegation.configure(
        principal(), "preview", enabled, version, "Owner backlog delivery"
    )
    args = {
        "request_key": key or f"configuration-{version}",
        "expected_preview_sha256": preview["preview_sha256"],
    }
    result = delegation.configure(
        principal(), "apply", enabled, version, "Owner backlog delivery", **args
    )
    return preview, result, args


def test_standing_preview_apply_is_default_off_and_replay_safe(db):
    with Session(db) as session:
        assert delegation.status(session)["enabled"] is False
        before = session.get(FactoryControl, "factory").policy_json
    preview, applied, args = configure(db)
    assert preview["binding"]["owner"] == OWNER
    assert preview["max_derivations"] == preview["max_pr_derivations"] == 3
    assert preview["allow_preexisting_owner_prs"] is True
    assert (
        delegation.configure(
            principal(), "apply", True, 0, "Owner backlog delivery", **args
        )
        == applied
    )
    with Session(db) as session:
        assert session.get(FactoryControl, "factory").policy_json == before
        assert delegation.status(session)["enabled"] is True
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.action == delegation.CONTROL
                    )
                ).all()
            )
            == 1
        )
        assert session.exec(select(FactoryReceipt)).all() == []
    with pytest.raises(ValueError, match="request_conflict"):
        delegation.configure(
            principal(), "apply", False, 0, "Owner backlog delivery", **args
        )


@pytest.mark.parametrize(
    "kind,authority",
    [
        (PrincipalKind.HUMAN, Authority.DELEGATED),
        (PrincipalKind.WORKLOAD, Authority.STANDING),
    ],
)
def test_delegated_or_nonhuman_caller_has_no_standing_authority(db, kind, authority):
    actor = replace(principal(), kind=kind, authority=authority)
    with pytest.raises(ValueError, match="standing_human_required"):
        delegation.configure(actor, "preview", True, 0, "No delegated authority")


@pytest.mark.parametrize(
    "action",
    [
        "owner_pr_control_applied",
        "configure",
        "pause_admissions",
        "refill_control_applied",
        "problem_issues_control_applied",
    ],
)
def test_restoring_identical_policy_cannot_revive_authority(db, action):
    configure(db)
    with controls._locked_session() as (session, control):
        controls._audit(session, "operator", action, ok=True)
        control.version += 1
    with Session(db) as session, pytest.raises(ValueError, match="policy_changed"):
        delegation._authority(session)
    configure(db)
    with Session(db) as session:
        assert delegation.status(session)["enabled"] is True


def landing_off_on(engine):
    """Exercise the actual version-bound operator landing-only control."""
    results = []
    for action in ("disable_auto_merge", "enable_auto_merge"):
        with Session(engine) as session:
            version = session.get(FactoryControl, "factory").version
        result = controls.request_control(
            action,
            "operator",
            request_key=f"{action}:{version}",
            expected_version=version,
        )
        assert result["ok"] and result["changed"], result
        assert (
            controls.request_control(
                action,
                "operator",
                request_key=f"{action}:{version}",
                expected_version=version,
            )
            == result
        )
        results.append(result)
    return results


@pytest.mark.parametrize("owner", ["owner_pr", "repair"])
def test_real_policy_toggle_never_resurrects_standing_authority(db, owner):
    from factory.orchestration import repair_landing_delegation as repair

    module = delegation if owner == "owner_pr" else repair

    def activate():
        with Session(db) as session:
            version = session.get(FactoryControl, "factory").version
        args = (
            {"mode": "ongoing", "duration_minutes": None} if owner == "repair" else {}
        )
        preview = module.configure(
            principal(), "preview", True, version, "Standing scope", **args
        )
        return module.configure(
            principal(),
            "apply",
            True,
            version,
            "Standing scope",
            **args,
            request_key=f"standing:{version}",
            expected_preview_sha256=preview["preview_sha256"],
        )

    applied = activate()
    # Failed versions and genuine no-op flag requests do not revoke authority.
    failed = controls.request_control(
        "disable_auto_merge", "operator", request_key="stale", expected_version=0
    )
    assert not failed["ok"]
    noop = controls.request_control(
        "enable_auto_merge",
        "operator",
        request_key="noop",
        expected_version=applied["version"],
    )
    assert noop["ok"] and not noop["changed"]
    with Session(db) as session:
        first = module._authority(session)[0].id
    landing_off_on(db)
    with (
        Session(db) as session,
        pytest.raises(ValueError, match="policy_changed|policy_replaced"),
    ):
        module._authority(session)
    activate()
    with Session(db) as session:
        assert module._authority(session)[0].id != first


@pytest.mark.parametrize("action", ["configure", "set_auto_merge"])
def test_failed_policy_audit_does_not_revoke_but_legacy_unknown_does(db, action):
    configure(db)
    with controls._locked_session() as (session, _):
        controls._audit(session, "operator", action, ok=False, reason="invalid")
    with Session(db) as session:
        assert delegation.status(session)["enabled"] is True
    with controls._locked_session() as (session, _):
        controls._audit(session, "operator", action)
    with Session(db) as session:
        assert delegation.status(session)["enabled"] is False


@pytest.mark.parametrize("queued", [False, True])
def test_native_repair_grant_is_not_revived_by_real_landing_toggle(
    tmp_path, monkeypatch, queued
):
    from factory.orchestration import factory_conductor_test as fixtures
    from factory.orchestration import factory_landing as landing
    from factory.orchestration import repair_landing_delegation as repair

    fixture = fixtures.feedback_db.__wrapped__(tmp_path, monkeypatch)
    engine = next(fixture)
    try:
        case = fixtures.native_repair_landing_case(monkeypatch, mode="ongoing")
        if queued:
            landing.landing_tick(case.live)
            assert case.queue == {"repair-pr"}
        else:
            assert (
                repair.prepare(case.repair["id"], 72, case.pull["head"]["sha"]) is None
            )
        with Session(engine) as session:
            authorization_id = repair._authority(session)[0].id
        landing_off_on(engine)
        for _ in range(2):
            landing.landing_tick(case.live)
            assert not case.queue and case.pull["auto_merge"] is None
        with Session(engine) as session:
            assert repair._latest(session, repair.CONTROL).id == authorization_id
            assert (
                len(
                    session.exec(
                        select(FactoryAudit).where(FactoryAudit.action == repair.GRANT)
                    ).all()
                )
                == 1
            )
    finally:
        fixture.close()


def test_owner_policy_change_and_revoke_fence_existing_authority(db):
    configure(db)
    with controls._locked_session() as (_, control):
        changed = json.loads(control.policy_json)
        changed["intake"]["owner_pr_author"] = {**OWNER, "id": 7}
        control.policy_json = dumps(changed)
    with Session(db) as session, pytest.raises(ValueError, match="policy_changed"):
        delegation._authority(session)
    configure(db, False)
    with Session(db) as session:
        assert delegation.status(session)["enabled"] is False


def candidate_stub(monkeypatch):
    state = {"task_id": "task-1", "receipt_id": 1}

    def candidate(session, _row, number, head):
        authority, proposal, _ = delegation._authority(session)
        return {
            "authorization_id": authority.id,
            "binding": proposal["binding"],
            **state,
            "repo": delegation.REPO,
            "pr_number": number,
            "head_sha": head,
            "canonical_key": delegation._canonical(delegation.REPO, number),
        }

    monkeypatch.setattr(delegation, "_candidate", candidate)
    return state


def test_replay_is_free_and_pr_cap_survives_reauthorization_and_receipt_change(
    db, monkeypatch
):
    state = candidate_stub(monkeypatch)
    configure(db)
    for n in range(3):
        assert delegation.prepare("task-1", 100, str(n) * 40) is None
    assert delegation.prepare("task-1", 100, "2" * 40) is None
    assert delegation.prepare("task-1", 100, "3" * 40) == "owner_pr_landing_pr_limit"
    configure(db, False)
    configure(db)
    state.update(task_id="task-2", receipt_id=2)
    assert delegation.prepare("task-2", 100, "4" * 40) == "owner_pr_landing_pr_limit"
    with Session(db) as session:
        assert (
            len(
                delegation._grants(
                    session, canonical=delegation._canonical(delegation.REPO, 100)
                )
            )
            == 3
        )


def test_rolling_cap_crosses_authorizations_and_resumes_without_refund(db, monkeypatch):
    candidate_stub(monkeypatch)
    configure(db)
    for number in (100, 101, 102):
        assert delegation.prepare("task-1", number, HEAD) is None
    configure(db, False)
    configure(db)
    assert delegation.prepare("task-1", 103, HEAD) == "owner_pr_landing_rolling_limit"
    with Session(db) as session:
        for event in session.exec(
            select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
        ).all():
            event.created_at = NOW - timedelta(hours=25)
        session.commit()
    assert delegation.prepare("task-1", 103, HEAD) is None
    with Session(db) as session:
        assert (
            len(
                session.exec(
                    select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
                ).all()
            )
            == 4
        )


def received_owner(db, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    pull = {
        "number": 100,
        "title": "Existing private fix",
        "body": "Closes #1",
        "state": "open",
        "draft": False,
        "user": {**OWNER, "type": "User"},
        "head": {
            "ref": "fix/preexisting",
            "sha": HEAD,
            "repo": {"full_name": delegation.REPO},
        },
        "base": {
            "ref": "main",
            "sha": "b" * 40,
            "repo": {"full_name": delegation.REPO},
        },
    }
    issue = {
        "number": 1,
        "title": "Accepted fix",
        "body": "Repair private diagnostics",
        "state": "open",
        "labels": [{"name": "agent-ready"}],
        "assignees": [],
        "html_url": f"https://github.com/{delegation.REPO}/issues/1",
    }

    def read(_repo, path):
        return pull if path.startswith("pulls/") else issue

    monkeypatch.setattr(conductor, "github_get", read)
    monkeypatch.setattr(conductor, "github_list", lambda *_: [])
    result = owner_prs.receive(
        delegation.REPO, 100, {**policy(), "owner_pr_issue_number": 1}, generation=1
    )
    with Session(db) as session:
        row = session.get(FactoryReceipt, result["receipt"]["id"])
        row.task_id, row.state = "task-1", "landing"
        session.commit()
    return SimpleNamespace(receipt_id=result["receipt"]["id"], pull=pull, issue=issue)


def test_real_owner_intake_before_activation_supplies_provenance(db, monkeypatch):
    case = received_owner(db, monkeypatch)
    configure(db)
    with Session(db) as session:
        authority, proposal, _ = delegation._authority(session)
        row = session.get(FactoryReceipt, case.receipt_id)
        provenance = delegation._provenance(session, row, 100, proposal)
        assert provenance["intake_audit_id"] < authority.id
        assert provenance["branch"] == "fix/preexisting"
        assert provenance["direct_refinement"] is None
        assert provenance["history"] == []


@pytest.mark.parametrize("hold", ["local", "deferred", "missing_event", "adoption"])
def test_provenance_refuses_local_held_or_legacy_receipts(db, monkeypatch, hold):
    case = received_owner(db, monkeypatch)
    configure(db)
    with Session(db) as session:
        row = session.get(FactoryReceipt, case.receipt_id)
        item = session.get(WorkItem, row.work_item_id)
        if hold == "local":
            item.authority = "local"
        if hold == "deferred":
            item.state = "deferred"
        if hold == "adoption":
            row.direction_json = dumps({})
        if hold == "missing_event":
            for event in session.exec(
                select(FactoryAudit).where(FactoryAudit.action == "receive_owner_pr")
            ).all():
                session.delete(event)
        session.commit()
        _, proposal, _ = delegation._authority(session)
        with pytest.raises(ValueError, match="held|provenance_missing"):
            delegation._provenance(session, row, 100, proposal)


@pytest.mark.parametrize("grant", [None, {}, {"schema_version": 1}])
def test_explicit_human_grant_never_falls_into_machine_derivation(
    db, monkeypatch, grant
):
    case = received_owner(db, monkeypatch)
    configure(db)
    with Session(db) as session:
        row = session.get(FactoryReceipt, case.receipt_id)
        direction = json.loads(row.direction_json)
        direction["landing_authorization"] = grant
        row.direction_json = dumps(direction)
        session.commit()
    assert (
        delegation.prepare("task-1", 100, HEAD)
        == "owner_pr_landing_operator_grant_owned"
    )


@pytest.mark.parametrize(
    "hold", ["label", "assignment", "body", "discussion", "scope_changed"]
)
def test_fresh_canonical_objective_checks_preserve_late_holds(db, monkeypatch, hold):
    from factory.orchestration import factory_conductor as conductor

    case = received_owner(db, monkeypatch)
    configure(db)
    if hold == "label":
        case.issue["labels"].append({"name": "human"})
    if hold == "assignment":
        case.issue["assignees"] = [{"login": "joe"}]
    if hold == "body":
        case.issue["body"] = "Do not merge until authorized"
    if hold == "scope_changed":
        case.pull["body"] = "Routine fix without closing association"
    monkeypatch.setattr(
        conductor,
        "github_list",
        lambda _repo, path: (
            [{"body": "Leave unmerged"}]
            if hold == "discussion" and path.startswith("issues/1/comments")
            else []
        ),
    )
    with Session(db) as session:
        row = session.get(FactoryReceipt, case.receipt_id)
        provenance = {"direct_refinement": None}
        with pytest.raises(ValueError, match="held|hold|scope_changed"):
            delegation._fresh_context(session, row, 100, case.pull, provenance)


@pytest.mark.parametrize("hold", ["label", "discussion", "local", "unknown", "claimed"])
def test_new_reference_cannot_bypass_holds_or_unknown_execution(db, monkeypatch, hold):
    from factory.orchestration import factory_conductor as conductor

    case = received_owner(db, monkeypatch)
    case.pull["body"] += "\nRefs #2"
    reference = {
        **case.issue,
        "number": 2,
        "labels": [{"name": "needs-human"}] if hold == "label" else [],
    }

    def read(_repo, path):
        if path == "issues/2":
            if hold == "unknown":
                raise ValueError("reference observation unavailable")
            return reference
        return case.pull if path.startswith("pulls/") else case.issue

    monkeypatch.setattr(conductor, "github_get", read)
    monkeypatch.setattr(
        conductor,
        "github_list",
        lambda _repo, path: (
            [{"body": "Do not merge"}]
            if hold == "discussion" and path.startswith("issues/2/comments")
            else []
        ),
    )
    with Session(db) as session:
        if hold == "local":
            session.add(
                WorkItem(
                    title="Local objective",
                    state="ready",
                    source_kind="factory",
                    trust="trusted",
                    authority="local",
                    github_repo=delegation.REPO,
                    github_issue_number=2,
                )
            )
        if hold == "claimed":
            session.add(
                FactoryReceipt(
                    repo=delegation.REPO,
                    issue_number=2,
                    generation=0,
                    title="History",
                    body="",
                    url="https://github.com/example",
                    actor=controls.INTAKE_ACTOR,
                    task_id="task-2",
                    state="failed",
                )
            )
            native = AgentSession(
                local_session_id="factory:task-2:implement_fix:1",
                workspace="/workspace/src",
                branch="fix/other",
                status="completed",
            )
            session.add(native)
            session.flush()
            session.add(
                PendingMessage(
                    session_id=native.id,
                    seq=2,
                    message_text="Continue",
                    claimed_by_replica="live-owner",
                    claimed_at=NOW - timedelta(days=2),
                )
            )
        session.commit()
        row = session.get(FactoryReceipt, case.receipt_id)
        with pytest.raises(
            ValueError, match="held|hold|scope_changed|unavailable|unsettled"
        ):
            delegation._fresh_context(
                session, row, 100, case.pull, {"direct_refinement": None}
            )


@pytest.mark.parametrize("execution", ["claimed_message", "uncertain_start"])
def test_terminal_looking_receipt_does_not_prove_execution_ceased(
    db, monkeypatch, execution
):
    received_owner(db, monkeypatch)
    configure(db)
    with Session(db) as session:
        if execution == "claimed_message":
            native = AgentSession(
                local_session_id="factory:task-1:implement_fix:1",
                workspace="/workspace/src",
                branch="fix/preexisting",
                status="completed",
            )
            session.add(native)
            session.flush()
            session.add(
                PendingMessage(
                    session_id=native.id,
                    seq=2,
                    message_text="Continue",
                    claimed_by_replica="live-owner",
                    claimed_at=NOW - timedelta(days=2),
                )
            )
        else:
            session.add(
                FactoryStart(
                    task_id="task-1",
                    start_key="uncertain",
                    actor="factory",
                    model="astra",
                    status="uncertain",
                    max_cost_usd=10,
                )
            )
        session.commit()
    assert (
        delegation.prepare("task-1", 100, HEAD)
        == "owner_pr_landing_execution_unsettled"
    )
    with Session(db) as session:
        assert (
            session.exec(
                select(FactoryAudit).where(FactoryAudit.action == delegation.GRANT)
            ).all()
            == []
        )


def test_terminal_hold_on_same_pr_under_another_issue_remains_authoritative(
    db, monkeypatch
):
    case = received_owner(db, monkeypatch)
    configure(db)
    with Session(db) as session:
        current = session.get(FactoryReceipt, case.receipt_id)
        session.add(
            FactoryReceipt(
                repo=delegation.REPO,
                issue_number=2,
                generation=0,
                title="Held historical owner",
                body="",
                url="https://github.com/example",
                actor=controls.INTAKE_ACTOR,
                task_id="task-2",
                state="cancelled",
                direction_json=current.direction_json,
            )
        )
        session.commit()
        _, proposal, _ = delegation._authority(session)
        with pytest.raises(ValueError, match="canonical_history_held"):
            delegation._provenance(session, current, 100, proposal)
