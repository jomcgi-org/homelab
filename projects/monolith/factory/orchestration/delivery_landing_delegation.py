"""Bounded standing authorization for landing ordinary factory deliveries.

Repair and owner-PR delegations cover narrow allowlists. An ordinary delivery
admitted through intake otherwise waits for a per-head operator grant, which
strands independently approved work. This default-off owner derives exact-head
authority from the typed independent review, inside a fixed path deny-list and
an operator-set rolling cap.

No worker receives operator identity. The existing audit journal holds both the
standing authorization and separate, exact-head machine derivations. Absence,
revocation, deny-rule change or policy drift fails closed. This module never
merges, releases holds, resumes execution or changes admission policy.
"""

import json
import re
from dataclasses import asdict
from datetime import timedelta

import httpx
import jsonschema
from sqlmodel import select

from factory.access import is_operator
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration.factory_models import FactoryAudit, FactoryControl
from factory.orchestration.models import SwarmNodeRun, SwarmTask
from factory.orchestration.repair_landing_delegation import SCOPE_SCHEMA

CONTROL = "delivery_landing_delegation_configured"
GRANT = "delivery_landing_authorized"
ACTOR = "factory:delivery-landing"
REPO = "jomcgi-org/homelab"
PATH_PROFILE = "private_deny_v1"
MAX_FILES = 40
MAX_LANDINGS = 50
MAX_TASK_DERIVATIONS = 3
RATE_WINDOW = timedelta(hours=24)
REPAIR_MARKER = "<!-- factory-problem:"

# Everything outside projects/ (CI, Bazel tooling, docs, root instructions)
# stays with an operator. Inside it, the boundary Joe set on 2026-11-10 excludes
# the factory and its framework packages (core, shared, app, framework, agent*,
# swarm), deployment and schema, credential and isolation code, and public
# routes. Domain packages the factory imports, such as knowledge, stay eligible. Other private project code is
# eligible: the typed review's effect flags, required CI and the merge queue
# gate every such merge. Do not widen these rules into whole projects or
# domains without his decision; that made the delegation match almost nothing.
DENY_RULES = {
    "root": "projects/",
    "prefixes": [
        "projects/embervm/control/lib/embervm/artifact_principal",
        "projects/embervm/control/lib/embervm/customer_kms",
        "projects/embervm/control/lib/embervm/key_service",
        "projects/embervm/control/lib/embervm/s3_client/",
        "projects/embervm/noded/egress/",
        "projects/embervm/noded/fcvm/driver/",
        "projects/embervm/xds/",
        "projects/firecracker/substrate/egress-proxy/",
        "projects/gke-apps/",
        "projects/gke-cluster/",
        "projects/monolith-public/",
        "projects/monolith/agent/",
        "projects/monolith/agent_kubernetes/",
        "projects/monolith/agent_sessions/",
        "projects/monolith/app/",
        "projects/monolith/artifact/",
        "projects/monolith/core/",
        "projects/monolith/faas/",
        "projects/monolith/factory/",
        "projects/monolith/framework/",
        "projects/monolith/personal_retrieval_audit/",
        "projects/monolith/sandbox/",
        "projects/monolith/shared/",
        "projects/monolith/swarm/",
        "projects/operators/",
        "projects/platform-gke/",
        "projects/platform/",
    ],
    "paths": [
        "projects/monolith/ARCHITECTURE.md",
        "projects/monolith/campsites/__init__.py",
        "projects/monolith/dr_jobs/__init__.py",
        "projects/monolith/frontend/BUILD",
        "projects/monolith/frontend/src/build-exclusions.test.js",
        "projects/monolith/frontend/src/lib/cache-headers.js",
        "projects/monolith/frontend/svelte.config.js",
        "projects/monolith/frontend/vite.config.js",
        "projects/monolith/frontend/vitest.config.js",
        "projects/monolith/goosecracker/repo_catalog.py",
        "projects/monolith/goosecracker/tests/repo_catalog_test.py",
        "projects/monolith/grimoire/__init__.py",
        "projects/monolith/grimoire_chat/__init__.py",
        "projects/monolith/hikes/__init__.py",
        "projects/monolith/home/__init__.py",
        "projects/monolith/import_boundaries_test.py",
        "projects/monolith/knowledge/__init__.py",
        "projects/monolith/knowledge/retrieval_policy.py",
        "projects/monolith/observability/__init__.py",
        "projects/monolith/observability/factory_goals.py",
        "projects/monolith/observability_snapshot_grants_test.py",
        "projects/monolith/ships/__init__.py",
        "projects/monolith/stars/__init__.py",
        "projects/monolith/trips/__init__.py",
        "projects/monolith/worldcup/__init__.py",
    ],
    "segments": [
        "chart",
        "cluster",
        "crd",
        "crds",
        "deploy",
        "migrations",
        "overlays",
        "templates",
    ],
    "word_stems": [
        "auth",
        "credential",
        "egress",
        "grant",
        "hook",
        "jail",
        "kms",
        "kyverno",
        "login",
        "oauth",
        "oidc",
        "password",
        "public",
        "rbac",
        "redact",
        "secret",
        "sigv4",
        "spiffe",
        "token",
        "turnstile",
        "visib",
    ],
    "basenames": ["AGENTS.md", "CLAUDE.md", "SKILL.md", "buildbuddy.yaml"],
    "suffixes": [".bzl"],
}
STATUSES = frozenset({"added", "modified", "removed", "renamed"})


def denied(path):
    """Return whether a repository path needs a separate operator decision."""
    if (
        not isinstance(path, str)
        or not path.startswith(DENY_RULES["root"])
        or path != path.strip()
        or "\\" in path
    ):
        return True
    segments = path.split("/")
    if any(not part or part.startswith(".") for part in segments):
        return True
    lowered = path.lower()
    # Match stems against the plain lowercased words and against the camelCase
    # split, including acronym-led names, so sigV4, authToken, APIToken,
    # CodexLogin, redacted and passwords are all caught.
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", path)
    words = set(re.split(r"[^a-z0-9]+", lowered)) | set(
        re.split(r"[^a-z0-9]+", split.lower())
    )
    return (
        path.startswith(tuple(DENY_RULES["prefixes"]))
        or path in DENY_RULES["paths"]
        or not {part.lower() for part in segments[:-1]}.isdisjoint(
            DENY_RULES["segments"]
        )
        or segments[-1] in DENY_RULES["basenames"]
        or lowered.endswith(tuple(DENY_RULES["suffixes"]))
        or any(word.startswith(tuple(DENY_RULES["word_stems"])) for word in words)
    )


def _require(condition, reason):
    if not condition:
        raise ValueError("delivery_landing_" + reason)


def configure(
    principal,
    operation,
    enabled,
    expected_version,
    reason,
    *,
    max_landings=None,
    request_key=None,
    expected_preview_sha256=None,
):
    _require(is_operator(principal), "standing_human_required")
    _require(operation in {"preview", "apply"}, "operation_invalid")
    _require(type(enabled) is bool, "enabled_invalid")
    _require(
        (not enabled and max_landings is None)
        or (
            enabled and type(max_landings) is int and 1 <= max_landings <= MAX_LANDINGS
        ),
        "limit_invalid",
    )
    request = dict(
        enabled=enabled,
        expected_version=expected_version,
        reason=controls._text(reason, "reason", 4000),
        max_landings=max_landings,
        request_key=request_key,
        expected_preview_sha256=expected_preview_sha256,
    )
    with controls._locked_session() as (db, control):
        if operation == "apply":
            controls._text(request_key, "request_key")
            for raw in db.exec(
                select(FactoryAudit.detail_json).where(
                    FactoryAudit.actor == principal.subject,
                    FactoryAudit.action == CONTROL,
                    FactoryAudit.detail_json.contains(
                        '"request_key":' + json.dumps(request_key)
                    ),
                )
            ).all():
                saved = json.loads(raw)
                if saved["request"]["request_key"] == request_key:
                    _require(saved["request"] == request, "request_conflict")
                    return saved["result"]
        _require(control.version == expected_version, "version_changed")
        policy = json.loads(control.policy_json or "{}")
        _require(
            not enabled
            or (
                control.state == "enabled"
                and policy.get("repo") == REPO
                and controls.repo_auto_merge_enabled(policy, REPO)
            ),
            "policy_not_enabled",
        )
        prior = controls.latest_audit(db, CONTROL)
        proposal = dict(
            enabled=enabled,
            standing_actor=principal.subject,
            version=control.version,
            policy_sha256=controls.digest(policy),
            repo=REPO,
            previous_authorization_id=prior.id if prior else None,
            path_profile=PATH_PROFILE,
            deny_rules=DENY_RULES,
            path_limitations=[
                "The deny-list excludes the factory and its framework packages "
                "(core, shared, app, framework, agent*, swarm), deployment, "
                "schema, credential, isolation and public-route paths. Domain "
                "packages the factory imports, such as knowledge, goosecracker "
                "and grimoire, stay eligible and run in the factory's process, "
                "so their safety relies on the typed independent review, "
                "required CI and the merge queue.",
                "Public domain route registration (each domain __init__.py in "
                "app/modules_public.py) is excluded. API and MCP modules inside "
                "eligible domains are not excluded by path; the typed review "
                "must set public_facing or security_sensitive for them.",
                "projects/monolith/BUILD is eligible because most monolith "
                "changes must edit it. It also declares image entrypoints and "
                "guard tests; review and required CI must preserve those. "
                "projects/monolith/ARCHITECTURE.md is excluded because it "
                "defines the delivery measure and stop condition.",
                "Binary (non-UTF-8) regular blobs remain eligible. Immutable "
                "tree checks bind blob identities and modes but do not inspect "
                "content or encoding. A typed review unable to assess that "
                "content cannot attest private_internal_reversible.",
            ],
            max_files=MAX_FILES,
            max_landings=max_landings,
            max_landings_limit=MAX_LANDINGS,
            rolling_window_hours=24,
            max_task_derivations=MAX_TASK_DERIVATIONS,
            reason=request["reason"],
        )
        proposal["preview_sha256"] = controls.digest(proposal)
        if operation == "preview":
            return {"ok": True, **proposal}
        _require(
            proposal["preview_sha256"] == expected_preview_sha256, "preview_changed"
        )
        control.version += 1
        control.actor, control.updated_at = principal.subject, controls._now()
        result = dict(
            ok=True, enabled=enabled, version=control.version, dispatched=False
        )
        controls._audit(
            db,
            principal.subject,
            CONTROL,
            request=request,
            proposal=proposal,
            result=result,
        )
        return result


def _authority(db):
    event = controls.latest_audit(db, CONTROL)
    _require(event is not None, "disabled")
    saved = json.loads(event.detail_json)
    proposal, result = saved["proposal"], saved["result"]
    _require(result.get("ok") is True and result.get("enabled") is True, "disabled")
    # A rollout that changes the boundary or its bounds needs a fresh decision.
    _require(
        proposal.get("path_profile") == PATH_PROFILE
        and proposal.get("deny_rules") == DENY_RULES
        and proposal.get("max_files") == MAX_FILES
        and proposal.get("rolling_window_hours") == 24
        and proposal.get("max_task_derivations") == MAX_TASK_DERIVATIONS
        and proposal.get("max_landings_limit") == MAX_LANDINGS
        and type(proposal.get("max_landings")) is int
        and 1 <= proposal["max_landings"] <= MAX_LANDINGS,
        "bounds_changed",
    )
    control = db.get(FactoryControl, "factory")
    policy = json.loads(control.policy_json or "{}")
    _require(
        control.state == "enabled" and controls.repo_auto_merge_enabled(policy, REPO),
        "policy_not_enabled",
    )
    _require(
        proposal["repo"] == REPO
        and proposal["policy_sha256"] == controls.digest(policy),
        "policy_changed",
    )
    # Restoring earlier policy bytes cannot renew standing or derived authority.
    from factory.orchestration.factory_landing_release import latest_policy_transition

    transition = latest_policy_transition(db)
    _require(transition is None or transition.id <= event.id, "policy_replaced")
    return event, proposal, result


def _grants(db, *, task_id=None, since=None, limit=MAX_LANDINGS + 1):
    query = select(FactoryAudit).where(
        FactoryAudit.actor == ACTOR, FactoryAudit.action == GRANT
    )
    if task_id is not None:
        query = query.where(FactoryAudit.task_id == task_id)
    if since is not None:
        query = query.where(FactoryAudit.created_at > since)
    return [
        {**json.loads(row.detail_json), "task_id": row.task_id}
        for row in db.exec(query.order_by(FactoryAudit.id.desc()).limit(limit)).all()
    ]


def _rolling_grants(db):
    # All authorizations count. Reconfiguration cannot reset the rolling cap.
    return _grants(db, since=controls._now() - RATE_WINDOW)


def status(db):
    event = controls.latest_audit(db, CONTROL)
    if event is None:
        return {"enabled": False, "reason": "delivery_landing_disabled"}
    try:
        _, proposal, _ = _authority(db)
        used = len(_rolling_grants(db))
        return {
            "enabled": True,
            "authorization_id": event.id,
            "path_profile": PATH_PROFILE,
            "rolling_window_hours": 24,
            "max_files": MAX_FILES,
            "max_task_derivations": MAX_TASK_DERIVATIONS,
            "max_landings": proposal["max_landings"],
            "derivations_used": used,
            "derivations_remaining": max(0, proposal["max_landings"] - used),
        }
    except ValueError as exc:
        return {"enabled": False, "authorization_id": event.id, "reason": str(exc)}


def _issue(row):
    from factory.orchestration import (
        factory_funding as funding,
        factory_landing as landing,
    )

    issue = landing.github_get(row.repo, f"issues/{row.issue_number}")
    labels = {
        (x.get("name", "") if isinstance(x, dict) else str(x)).lower()
        for x in issue.get("labels") or []
    }
    _require(
        issue.get("number") == row.issue_number
        and issue.get("state") == "open"
        and not issue.get("assignees")
        and not labels & funding.ROUTINE_HELD_LABELS,
        "issue_held",
    )
    return {"number": row.issue_number, "labels": sorted(labels)}


def _file_evidence(pull, base_sha, head):
    """Complete, immutable diff listing inside the deny-list boundary.

    The merge base and head commits already fix every byte. This proves the
    listing is complete, that no path is excluded, and that both old and new
    files are regular non-executable blobs. Symlinks, submodules and executable
    files need an operator, including when removed or replaced.
    """
    from factory.orchestration.factory_landing import github_get

    repo = pull["base"]["repo"]["full_name"]
    count = pull["changed_files"]
    _require(type(count) is int and 1 <= count <= MAX_FILES, "file_limit")
    files = github_get(repo, f"compare/{base_sha}...{head}")["files"]
    paths = [f["filename"] for f in files]
    _require(len(files) == count and len(set(paths)) == count, "files_incomplete")
    for f in files:
        _require(f["status"] in STATUSES, "file_status_requires_operator")
        previous = f.get("previous_filename")
        _require(
            (f["status"] == "renamed") == (previous is not None),
            "file_status_requires_operator",
        )
        _require(
            not denied(f["filename"]) and (previous is None or not denied(previous)),
            "paths_require_operator",
        )
    cache = {}
    roots = {
        commit: github_get(repo, f"git/commits/{commit}")["tree"]["sha"]
        for commit in (base_sha, head)
    }

    def blob(commit, path, *, present):
        """Walk non-recursive immutable trees; require absence when appropriate."""
        tree_sha, parts = roots[commit], path.split("/")
        for index, part in enumerate(parts):
            if tree_sha not in cache:
                tree = github_get(repo, f"git/trees/{tree_sha}")
                _require(tree.get("truncated") is False, "files_incomplete")
                cache[tree_sha] = tree["tree"]
            matches = [e for e in cache[tree_sha] if e["path"] == part]
            if not matches and not present:
                return None
            _require(len(matches) == 1, "files_incomplete")
            entry = matches[0]
            if index < len(parts) - 1:
                _require(
                    entry["type"] == "tree" and entry["mode"] == "040000",
                    "file_type_requires_operator",
                )
                tree_sha = entry["sha"]
            else:
                _require(
                    entry["type"] == "blob" and entry["mode"] == "100644",
                    "file_type_requires_operator",
                )
                _require(present, "files_incomplete")
                return entry["sha"]

    evidence = []
    for f in sorted(files, key=lambda f: f["filename"]):
        path = f["filename"]
        old_path = f.get("previous_filename", path)
        old = blob(base_sha, old_path, present=f["status"] != "added")
        new = blob(head, path, present=f["status"] != "removed")
        _require(
            (old if f["status"] == "removed" else new) == f["sha"],
            "file_type_requires_operator",
        )
        if f["status"] == "renamed":
            blob(base_sha, path, present=False)
            blob(head, old_path, present=False)
        evidence.append(
            {
                "path": path,
                "previous_path": old_path if f["status"] == "renamed" else None,
                "base_blob_sha": old,
                "head_blob_sha": new,
            }
        )
    return {"base_sha": base_sha, "head_sha": head, "files": evidence}


def _candidate(db, row, number, head):
    from factory import review_publisher
    from factory.orchestration import (
        factory_funding as funding,
        factory_landing as landing,
    )
    from factory.orchestration import factory_landing_release as releases

    authority, proposal, result = _authority(db)
    _require(
        row is not None and row.repo == REPO and row.state == "landing",
        "receipt_ineligible",
    )
    direction = json.loads(row.direction_json or "{}")
    # Each of these has its own authority owner. Never fall through to this one.
    _require(
        isinstance(direction, dict)
        and "landing_authorization" not in direction
        and "owner_pr" not in direction
        and REPAIR_MARKER not in (row.body or ""),
        "other_owner",
    )
    graph._lock_task(db, row.task_id)
    _require(
        not funding.routine_hold(db, row, row.task_id)
        and not funding._recovery_held(db, row.task_id)
        and not controls.decision_in_flight(db, row.id),
        "receipt_held",
    )
    refusal = releases.execution_refusal(db, row.task_id)
    if refusal:
        raise ValueError("delivery_landing_" + refusal)
    state = landing._landing_state(db, [row.task_id])[row.task_id]
    _require(
        not state["merge_arm_refused"]
        and len(state["merge_ejected"]) < landing.MAX_EJECTIONS,
        "recovery_limit",
    )
    issue = _issue(row)
    review = review_publisher.collect(row.task_id, session=db)
    _require(
        isinstance(review, review_publisher.ReviewEvidence)
        and (review.repo, review.pr_number, review.head_sha)
        == (row.repo, number, head),
        "independent_review_missing",
    )
    run = db.get(SwarmNodeRun, review.review_run_id)
    valid, artifact = review_publisher._artifact(run)
    _require(valid == "ok", "review_invalid")
    scope = artifact.get("landing_scope")
    try:
        jsonschema.validate(scope, SCOPE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValueError("delivery_landing_scope_missing_or_invalid") from exc
    _require(
        scope["classification"] == "private_internal_reversible"
        and not any(
            scope[key]
            for key in (
                "public_facing",
                "security_sensitive",
                "authentication_or_credentials",
                "destructive",
            )
        ),
        "scope_requires_operator",
    )
    pull = landing.github_get(row.repo, f"pulls/{number}")
    task = db.get(SwarmTask, row.task_id)
    # Only a branch the factory created for this exact task qualifies.
    _require(
        pull.get("number") == number
        and pull.get("head", {}).get("sha") == head
        and pull.get("state") == "open"
        and not pull.get("merged")
        and not pull.get("draft")
        and pull.get("head", {}).get("ref") == f"factory/{row.task_id}"
        and pull.get("head", {}).get("repo", {}).get("full_name") == row.repo
        and pull.get("base", {}).get("repo", {}).get("full_name") == row.repo
        and pull.get("base", {}).get("ref") == task.base_branch,
        "pull_identity_changed",
    )
    # The base tip advances independently of this PR. Bind the reviewed diff to
    # its immutable merge base; a changed merge base still needs a new review.
    comparison = landing.github_get(row.repo, f"compare/{pull['base']['sha']}...{head}")
    _require(
        scope["base_sha"] == comparison["merge_base_commit"]["sha"],
        "review_scope_changed",
    )
    files = _file_evidence(pull, scope["base_sha"], head)
    _require(
        sorted(scope["paths"]) == [f["path"] for f in files["files"]],
        "review_scope_changed",
    )
    # A successful pr-checks status and every non-advisory context are required
    # here. The protected queue remains the final CI owner.
    releases._handoff_ci(row.repo, head)
    return dict(
        authorization_id=authority.id,
        standing_actor=authority.actor,
        delegation_version=result["version"],
        policy_sha256=proposal["policy_sha256"],
        receipt_id=row.id,
        task_id=row.task_id,
        repo=row.repo,
        pr_number=number,
        head_sha=head,
        direction_sha256=controls.digest(row.direction_json),
        issue=issue,
        review={k: v for k, v in asdict(review).items() if k != "published_check_id"},
        review_sha256=controls.digest(artifact),
        scope=scope,
        files=files,
    )


def _refusal(exc):
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, httpx.HTTPError):
        return "delivery_landing_observation_unavailable"
    return "delivery_landing_evidence_invalid"


def prepare(task_id, number, head):
    """Landing only: derive once under the same lock as revocation and arming."""
    try:
        with controls._locked_session() as (db, _):
            if controls.latest_audit(db, CONTROL) is None:
                return "scoped_landing_authorization_missing"
            candidate = _candidate(db, controls._receipt(db, task_id), number, head)
            grants = _grants(db, task_id=task_id)
            if candidate in grants:
                return None
            _, proposal, _ = _authority(db)
            _require(len(grants) < MAX_TASK_DERIVATIONS, "task_limit")
            _require(
                len(_rolling_grants(db)) < proposal["max_landings"], "rolling_limit"
            )
            controls._audit(
                db,
                ACTOR,
                GRANT,
                task_id=task_id,
                **{key: value for key, value in candidate.items() if key != "task_id"},
            )
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _refusal(exc)


def refusal(db, row, number, head):
    if controls.latest_audit(db, CONTROL) is None:
        return "scoped_landing_authorization_missing"
    try:
        candidate = _candidate(db, row, number, head)
        _require(
            candidate in _grants(db, task_id=row.task_id),
            "authorization_missing_or_changed",
        )
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _refusal(exc)
