"""Explicit operator release of a staged delivery hold, without accepting delivery.

The preview binds the owned PR head and current receipt state. Application only
removes that hold or refreshes its audited authorization after a head change,
leaving execution, review, CI and rollout gates unchanged.
"""

import base64
import hashlib
import json
import re

from sqlmodel import select

from core.jsonio import dumps
from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

ACTION = "operator_landing_hold_released"
SCOPE_ACTION = "operator_scoped_landing_authorized"
PUBLIC_DOCS = frozenset(
    {
        "projects/monolith/ARCHITECTURE.md",
        "projects/monolith/factory/orchestration/FACTORY.md",
    }
)


def _scope(value):
    """An operator's explicit assessment, never an automatic classifier."""
    if (
        not isinstance(value, dict)
        or set(value) != {"classification", "evidence"}
        or value.get("classification")
        not in ("private_internal_reversible", "public_docs")
    ):
        raise ValueError("landing_scope_unsupported")
    return {
        "classification": value["classification"],
        "evidence": controls._text(value.get("evidence"), "scope evidence", 4000),
    }


def _public_docs(pull):
    """Immutable GitHub evidence, not a caller-supplied docs classification.

    Only edits to two existing contracts qualify. Modes and UTF-8 content are
    checked as well as paths: renames, executable files, symlinks, binary files
    and incomplete responses must not turn this into a generic public grant.
    """
    from factory.orchestration.factory_conductor import github_get

    def reject():
        raise ValueError("landing_public_docs_unverified")

    try:
        repo = pull["base"]["repo"]["full_name"]
        base, head = pull["base"]["sha"], pull["head"]["sha"]
        if not all(
            isinstance(s, str) and re.fullmatch(r"[0-9a-f]{40}", s)
            for s in (base, head)
        ):
            reject()
        count = pull["changed_files"]
        if type(count) is not int or not 1 <= count <= len(PUBLIC_DOCS):
            reject()
        files = github_get(repo, f"compare/{base}...{head}")["files"]
        paths = [f["filename"] for f in files]
        if (
            len(files) != count
            or len(set(paths)) != count
            or not set(paths) <= PUBLIC_DOCS
            or any(
                f["status"] != "modified" or f.get("previous_filename") for f in files
            )
        ):
            reject()
        # The repository-wide recursive tree exceeds the GitHub reader's byte
        # cap. Walk only the allowlisted paths, caching shared parent trees.
        trees, cache = [], {}
        for revision in (base, head):
            root = github_get(repo, f"git/commits/{revision}")["tree"]["sha"]
            entries = {}
            for path in paths:
                tree_sha = root
                parts = path.split("/")
                for index, part in enumerate(parts):
                    if tree_sha not in cache:
                        tree = github_get(repo, f"git/trees/{tree_sha}")
                        if tree.get("truncated") is not False:
                            reject()
                        cache[tree_sha] = tree["tree"]
                    matches = [e for e in cache[tree_sha] if e["path"] == part]
                    if len(matches) != 1:
                        reject()
                    entry = matches[0]
                    if index == len(parts) - 1:
                        if entry["type"] != "blob" or entry["mode"] != "100644":
                            reject()
                        entries[path] = entry
                    else:
                        if entry["type"] != "tree" or entry["mode"] != "040000":
                            reject()
                        tree_sha = entry["sha"]
            trees.append(entries)
        evidence = []
        for f in sorted(files, key=lambda f: f["filename"]):
            path = f["filename"]
            before, after = trees[0][path]["sha"], trees[1][path]["sha"]
            if after != f["sha"] or before == after:
                reject()
            blob = github_get(repo, f"git/blobs/{after}")
            if blob["encoding"] != "base64" or not 0 < blob["size"] <= 512 * 1024:
                reject()
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
            if len(raw) != blob["size"] or b"\x00" in raw:
                reject()
            raw.decode("utf-8")
            evidence.append(
                {"path": path, "base_blob_sha": before, "head_blob_sha": after}
            )
        return {"base_sha": base, "head_sha": head, "files": evidence}
    except (AttributeError, KeyError, TypeError, UnicodeError, base64.binascii.Error):
        reject()


def _observe(receipt_id, pr_number, head_sha, scope):
    pull = _pull(receipt_id, pr_number, head_sha)
    evidence = (
        _public_docs(pull)
        if scope and _scope(scope)["classification"] == "public_docs"
        else None
    )
    return pull, evidence


def scoped_refusal(db, row, pr_number, head_sha):
    """Require the exact operator-audited grant; old releases are not grants."""
    if row is None or row.task_paused or row.cancellation_requested:
        return "landing_receipt_unavailable"
    if row.state not in ("succeeded", "landing") or controls.landing_held(row):
        return "landing_receipt_held"
    if controls.review_recovery_held(row.task_id, session=db):
        return "review_only_recovery"
    try:
        grant = json.loads(row.direction_json or "{}").get("landing_authorization")
        if (
            not isinstance(grant, dict)
            or type(grant.get("schema_version")) is not int
            or grant.get("schema_version") != 1
        ):
            return "scoped_landing_authorization_missing"
        if any(
            grant.get(key) != value
            for key, value in {
                "receipt_id": row.id,
                "task_id": row.task_id,
                "repo": row.repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
            }.items()
        ):
            return "scoped_landing_authorization_identity_changed"
        if _scope(grant.get("scope")) != grant["scope"]:
            return "scoped_landing_authorization_scope_changed"
        if grant["scope"]["classification"] == "public_docs":
            proof = grant.get("public_docs", {})
            files = proof.get("files", [])
            if (
                proof.get("head_sha") != head_sha
                or not files
                or len(files) > len(PUBLIC_DOCS)
                or len({f["path"] for f in files}) != len(files)
                or any(f["path"] not in PUBLIC_DOCS for f in files)
            ):
                return "scoped_landing_public_docs_unverified"
        for event in db.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action == SCOPE_ACTION,
            )
        ).all():
            detail = json.loads(event.detail_json)
            request = detail.get("request", {})
            result = detail.get("result", {})
            if (
                event.actor == grant.get("actor")
                and request.get("request_key") == grant.get("request_key")
                and request.get("receipt_id") == row.id
                and request.get("pr_number") == pr_number
                and request.get("head_sha") == head_sha
                and request.get("scope") == grant["scope"]
                and result.get("ok") is True
                and result.get("landing_authorization") == grant
            ):
                return None
    except (AttributeError, KeyError, TypeError, ValueError):
        return "scoped_landing_authorization_invalid"
    return "scoped_landing_authorization_unaudited"


def _pull(receipt_id, pr_number, head_sha):
    from factory.orchestration.factory_conductor import github_get

    if type(pr_number) is not int or pr_number <= 0:
        raise ValueError("invalid_pr_number")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("invalid_head_sha")
    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None:
            raise ValueError("unknown_receipt")
        repo = row.repo
    # Network observations precede the control lock. Identity is checked again
    # against the locked receipt, and apply obtains a fresh observation.
    return github_get(repo, f"pulls/{pr_number}")


def _refreshable(db, row, pr_number, head_sha):
    """Only refresh a prior audited release for this receipt and the same PR."""
    direction = json.loads(row.direction_json or "{}")
    authorization = direction.get("landing_authorization")
    if (
        not isinstance(authorization, dict)
        or authorization.get("pr_number") != pr_number
        or authorization.get("head_sha") == head_sha
    ):
        return False
    for event in db.exec(
        select(FactoryAudit).where(
            FactoryAudit.action == ACTION, FactoryAudit.task_id == row.task_id
        )
    ).all():
        detail = json.loads(event.detail_json)
        request = detail.get("request", {})
        if (
            event.actor == authorization.get("actor")
            and request.get("receipt_id") == row.id
            and request.get("pr_number") == pr_number
            and request.get("head_sha") == authorization.get("head_sha")
            and request.get("request_key") == authorization.get("request_key")
            and detail.get("result", {}).get("ok") is True
        ):
            return True
    return False


def _preview(
    db,
    control,
    receipt_id,
    pr_number,
    head_sha,
    reason,
    pull,
    scope=None,
    public_docs_evidence=None,
):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_decisions import decision_in_flight

    reason = controls._text(reason, "reason", 4000)
    if scope is not None:
        scope = _scope(scope)
    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or not row.task_id
        or row.state not in (*controls._ACTIVE, "landing", "succeeded", "escalated")
        or row.cancellation_requested
        or control.state not in ("enabled", "paused")
        or (
            scope is None
            and not controls.landing_held(row)
            and not _refreshable(db, row, pr_number, head_sha)
        )
    ):
        raise ValueError("landing_release_requires_held_receipt")
    public_docs = scope is not None and scope["classification"] == "public_docs"
    if scope is not None and (
        (controls.landing_held(row) and not public_docs)
        or row.task_paused
        or row.state not in ("succeeded", "landing")
        or pull.get("state") != "open"
        or pull.get("merged")
        or pull.get("draft")
    ):
        raise ValueError("scoped_landing_requires_unheld_settled_delivery")
    if controls.review_recovery_held(row.task_id, session=db):
        raise ValueError("review_only_recovery")
    refusal = controls._quiescence_refusal(db, row.task_id)
    if refusal:
        raise ValueError("landing_release_" + refusal)
    if (
        db.exec(
            select(PendingMessage.id)
            .join(AgentSession)
            .where(
                AgentSession.local_session_id.startswith(
                    f"factory:{row.task_id}:", autoescape=True
                ),
                PendingMessage.claimed_by_replica.isnot(None),
            )
        ).first()
        is not None
    ):
        raise ValueError("landing_release_claimed_execution")
    if decision_in_flight(db, row.id):
        raise ValueError("landing_release_decision_in_flight")
    direction = json.loads(row.direction_json or "{}")
    policy = controls._effective_policy(db, row)
    identity = {
        **direction,
        "id": row.task_id,
        "repo": row.repo,
        "base_branch": policy["base_branch"],
    }
    granted = conductor.delivery_pr_number(identity)
    if (
        pull.get("number") != pr_number
        or pull.get("head", {}).get("sha") != head_sha
        or not conductor.delivery_identity_matches(identity, pull)
        or (granted is not None and granted != pr_number)
        or (pull.get("state") != "open" and pull.get("merged") is not True)
    ):
        raise ValueError("landing_release_pr_identity_changed")
    if controls.delivery_branch_owner(
        db, row.repo, conductor.delivery_branch(identity), exclude_receipt_id=row.id
    ):
        raise ValueError("landing_release_branch_owned_elsewhere")
    if public_docs and (
        not public_docs_evidence or public_docs_evidence["head_sha"] != head_sha
    ):
        raise ValueError("landing_public_docs_unverified")
    result = {
        "operation": "authorize"
        if scope is not None
        else ("release" if controls.landing_held(row) else "refresh"),
        "receipt_id": row.id,
        "task_id": row.task_id,
        "repo": row.repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "pr_merged": pull.get("merged") is True,
        "pr_draft": pull.get("draft") is True,
        "pr_body_sha256": hashlib.sha256((pull.get("body") or "").encode()).hexdigest(),
        "merge_commit_sha": pull.get("merge_commit_sha")
        if pull.get("merged")
        else None,
        "expected_version": control.version,
        "expected_revision": graph.current_version(row.task_id, session=db),
        "receipt_state": row.state,
        "receipt_updated_at": row.updated_at.isoformat(),
        "receipt_body_sha256": hashlib.sha256(row.body.encode()).hexdigest(),
        "stays_paused": row.task_paused,
        "direction": direction,
        "policy": policy,
        "reason": reason,
        "delivery_accepted": False,
    }
    if scope is not None:
        result["scope"] = scope
    if public_docs:
        result["public_docs"] = public_docs_evidence
        # Only this new scope may release and authorize atomically. Existing
        # private grants and legacy hold-release semantics stay unchanged.
        result["operation"] = (
            "release_and_authorize" if controls.landing_held(row) else "authorize"
        )
    result["preview_sha256"] = hashlib.sha256(dumps(result).encode()).hexdigest()
    return result


def preview(receipt_id, pr_number, head_sha, reason, *, scope=None):
    pull, docs = _observe(receipt_id, pr_number, head_sha, scope)
    with controls._locked_session() as (db, control):
        return _preview(
            db, control, receipt_id, pr_number, head_sha, reason, pull, scope, docs
        )


def _replay(db, actor, request):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action.in_((ACTION, SCOPE_ACTION)), FactoryAudit.actor == actor
        )
    ).all():
        previous = json.loads(raw)
        if previous["request"]["request_key"] == request["request_key"]:
            if previous["request"] != request:
                raise ValueError("conflicting_landing_release_request")
            if previous["result"].get("pending"):
                for applied_raw in db.exec(
                    select(FactoryAudit.detail_json).where(
                        FactoryAudit.action.in_(
                            (
                                "settled_landing_hold_released",
                                "execution_capacity_reacquired",
                            )
                        ),
                        FactoryAudit.task_id == previous["result"]["task_id"],
                    )
                ).all():
                    applied = json.loads(applied_raw)
                    authorization = applied.get("landing_authorization") or {}
                    if (
                        "landing_hold" in applied.get("intent", {})
                        and authorization.get("actor") == actor
                        and authorization.get("request_key") == request["request_key"]
                    ):
                        return {**previous["result"], "pending": False, "reason": None}
            return previous["result"]
    return None


def apply(
    receipt_id,
    pr_number,
    head_sha,
    reason,
    *,
    actor,
    request_key,
    expected_preview_sha256,
    scope=None,
):
    actor = controls._text(actor, "actor")
    request = {
        "receipt_id": receipt_id,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    if scope is not None:
        request["scope"] = _scope(scope)
    with controls._read_session() as db:
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
    pull, docs = _observe(receipt_id, pr_number, head_sha, scope)
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
        proposal = _preview(
            db, control, receipt_id, pr_number, head_sha, reason, pull, scope, docs
        )
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("landing_release_preview_changed")
        row = db.get(FactoryReceipt, receipt_id)
        direction = json.loads(row.direction_json or "{}")
        prior_hold = direction.get("landing_hold")
        prior_authorization = direction.get("landing_authorization")
        direction["landing_authorization"] = {
            "actor": actor,
            "request_key": request_key,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "reason": proposal["reason"],
            "authorized_at": controls._now().isoformat(),
        }
        if scope is not None:
            direction["landing_authorization"].update(
                schema_version=1,
                receipt_id=row.id,
                task_id=row.task_id,
                repo=row.repo,
                scope=proposal["scope"],
            )
        if "public_docs" in proposal:
            direction["landing_authorization"]["public_docs"] = proposal["public_docs"]
        row.direction_json = dumps(direction)
        release = {"ok": True, "pending": False, "reason": None}
        if proposal["operation"] in ("release", "release_and_authorize"):
            if row.state in controls._ACTIVE:
                release = controls.request_capacity_release(
                    db, control, row, actor, release_hold=True
                )
                if not release["ok"]:
                    raise ValueError(release["reason"])
            else:
                direction.pop("landing_hold")
                row.direction_json = dumps(direction)
        row.updated_at = controls._now()
        control.version += 1
        control.actor = actor
        control.updated_at = controls._now()
        result = {
            **release,
            "operation": proposal["operation"],
            "receipt_id": row.id,
            "task_id": row.task_id,
            "version": control.version,
            "stays_paused": row.task_paused,
            "delivery_accepted": False,
            "preview_sha256": proposal["preview_sha256"],
        }
        if scope is not None:
            result["landing_authorization"] = direction["landing_authorization"]
        controls._audit(
            db,
            actor,
            SCOPE_ACTION if scope is not None else ACTION,
            task_id=row.task_id,
            request=request,
            result=result,
            prior_hold=prior_hold,
            prior_authorization=prior_authorization,
        )
        return result
