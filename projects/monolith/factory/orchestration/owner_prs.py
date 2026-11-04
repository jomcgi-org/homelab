"""Owner PR discovery on the existing intake sweep, with canonical issue ownership.

Author identity permits discovery, never scope or merge authority. Accepted
issues or bounded scope refinement grant execution; canonical work items,
receipt history and landing owners remain authoritative. This module owns no scheduler or delivery ledger.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt, WorkItem

# Text can veto automatic action, never grant it. Historical staging directions
# need their existing supported release, not a newer generic autonomy prompt.
HOLD = re.compile(
    r"leave[ -]unmerged|keep\b[^\n.]{0,80}\b(?:draft|unmerged)|"
    r"do not merge|don.t merge|outside the merge queue|landing hold|"
    r"(?:merge|deployment)\b[^\n.]{0,60}\brequires? separate authorization",
    re.IGNORECASE,
)
# These complete absence assertions occurred in accepted native briefs. They
# describe an observation, never grant authority or release another hold. Keep
# the vocabulary deliberately finite rather than interpreting arbitrary prose.
_ABSENCE = re.compile(
    r"No explicit human, security or leave-unmerged hold was found in the PR "
    r"labels, discussion or reviews\."
    r"|Current discussion contains no explicit leave-unmerged hold\."
)
_QUALIFIER_WORDS = re.compile(
    r"\b(?:if|unless|until|when|once|provided|assuming|suppose|imagine|"
    r"but|however|except|although|yet|conditional|hypothetical|"
    r"say|says|said|write|writes|wrote|quote|quoted|quoting|example|template|"
    r"wording|phrase|sentence|claim|claims|claimed|alleged|incorrect|false|"
    r"untrue|fiction|contrary|wrong|disregard|ignore|retract|retracted|"
    r"withdraw|withdrawn|revoke|revoked|superseded|inaccurate|unreliable|"
    r"mistaken|mistake|invalid|nonbinding)\b|"
    r"\bnot (?:accurate|true|correct|binding|authoritative|valid|current)\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"'\u2018\u2019\u201c\u201d<>\\]|(?:^|\n)[ \t]{4}")
SHA = re.compile(r"[0-9a-f]{40}")
MAX_REFERENCES = 5
MAX_REFERENCE_READS = 20


class OwnerPRHold(ValueError):
    """Positive, non-mutating observation of an explicit ownership hold."""


def has_hold(body: str) -> bool:
    """Recognize two literal absence assertions, retaining text-veto backstops."""
    if not HOLD.search(body):
        return False
    # Code/HTML quotations can span paragraphs. Unknown presentation remains a
    # veto, even if another paragraph contains a recognized absence sentence.
    if re.search(
        r"```|~~~|<(?:blockquote|pre|code|q)\b|^[ \t]*[\"'\u201c\u2018]",
        body,
        re.IGNORECASE | re.MULTILINE,
    ):
        return True
    paragraphs = re.split(r"\n[ \t]*\n|\n(?=[ \t]*[-*] )", body)
    for index, paragraph in enumerate(paragraphs):
        if not HOLD.search(paragraph):
            continue
        # Qualifying prose immediately around a paragraph can introduce a
        # quote/example or retract the assertion without repeating the phrase.
        neighbors = (
            paragraphs[max(0, index - 1) : index] + paragraphs[index + 1 : index + 2]
        )
        context = "\n".join(
            [paragraph]
            + [
                part
                for part in neighbors
                if not part.lstrip().startswith(("- ", "* "))
                or part.rstrip().endswith(":")
            ]
        )
        if (
            len(paragraph) > 4096
            or _QUOTED.search(paragraph)
            or _QUALIFIER_WORDS.search(context)
        ):
            return True
        remaining = paragraph
        for match in reversed(list(_ABSENCE.finditer(paragraph))):
            prefix, suffix = paragraph[: match.start()], paragraph[match.end() :]
            # No clauses, quotations, inline code or sentence fragments. A
            # balanced earlier code span (e.g. a head SHA) is not this sentence.
            if (
                (prefix.strip() and not prefix.rstrip().endswith("."))
                or (suffix and not suffix[0].isspace())
                or prefix.count("`") % 2
            ):
                continue
            remaining = (
                remaining[: match.start()]
                + " " * len(match[0])
                + remaining[match.end() :]
            )
        if HOLD.search(remaining):
            return True
    return False


def closing_references(body: str, repo: str) -> set[tuple[str, int]]:
    """Parse explicit closing identities independently of a bounded listing."""
    pattern = (
        r"(?<![A-Za-z0-9_])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
        r"(?:(?:https?://github\.com/(?P<urlrepo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/)|"
        r"(?:(?P<shortrepo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#)(?P<number>\d+)\b"
    )
    return {
        ((m["urlrepo"] or m["shortrepo"] or repo).lower(), int(m["number"]))
        for m in re.finditer(pattern, body or "", re.IGNORECASE)
    }


def objective_references(body: str, repo: str) -> set[tuple[str, int]]:
    """References can veto discovery without granting execution authority."""
    pattern = (
        r"(?<![A-Za-z0-9_/])(?:(?:https?://github\.com/"
        r"(?P<urlrepo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/)|"
        r"(?:(?P<shortrepo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#)"
        r"(?P<number>\d+)\b"
    )
    return {
        ((m["urlrepo"] or m["shortrepo"] or repo).lower(), int(m["number"]))
        for m in re.finditer(pattern, body or "", re.IGNORECASE)
    }


def _resolve_references(pull, issues, repo, read_get, *, cache=None):
    """Bound missing-reference reads; a truncated listing cannot erase a hold."""
    references = objective_references(pull.get("body") or "", repo)
    if len(references) > MAX_REFERENCES:
        raise ValueError("referenced_objective_limit")
    if any(slug != repo for slug, _ in references):
        raise ValueError("scope_cross_repository")
    resolved = {item["number"]: item for item in issues}
    cache = {} if cache is None else cache
    for _, number in sorted(references):
        if number not in resolved:
            if number not in cache:
                if len(cache) >= MAX_REFERENCE_READS:
                    raise ValueError("referenced_objective_read_limit")
                # A failed read also consumes this sweep's bounded allowance.
                cache[number] = None
                cache[number] = read_get(repo, f"issues/{number}")
            item = cache[number]
            if not isinstance(item, dict) or item.get("number") != number:
                raise ValueError("referenced_objective_unavailable")
            resolved[number] = item
    return list(resolved.values())


def _referenced_history(db, repo: str, numbers: set[int]) -> str | None:
    for number in numbers:
        item = db.exec(
            select(WorkItem).where(
                WorkItem.github_repo == repo,
                WorkItem.github_issue_number == number,
            )
        ).one_or_none()
        if item and (
            item.authority == "local"
            or item.state in ("closed", "deferred", "needs_human", "done")
        ):
            return "referenced_objective_held"
    return None


def identity_matches(pull: object, policy: dict) -> bool:
    owner = controls.intake_policy(policy)["owner_pr_author"]
    if not owner or not isinstance(pull, dict):
        return False
    user = pull.get("user") or {}
    return (
        user.get("type") == "User"
        and type(user.get("id")) is int
        and user["id"] == owner["id"]
        and str(user.get("login", "")).lower() == owner["login"]
    )


def _hold(item: dict, excluded: set[str]) -> str | None:
    from factory.orchestration.factory_intake_loop import _label_names

    labels = _label_names(item)
    if item.get("assignees"):
        return "assigned"
    if labels & (
        excluded
        | {"human", "needs-human", "needs-thought", "security", "security-finding"}
    ):
        return "explicit_hold"
    if any(
        re.search(r"hold|blocked|do-not-merge|manual|agent-skip", x) for x in labels
    ):
        return "explicit_hold"
    if has_hold(item.get("body") or ""):
        return "explicit_hold"
    return None


def _identity_reason(pull: dict, repo: str, policy: dict) -> str | None:
    if not identity_matches(pull, policy):
        return "author_changed"
    head, base = pull.get("head") or {}, pull.get("base") or {}
    if pull.get("state") != "open":
        return "not_open"
    if (
        type(pull.get("number")) is not int
        or pull["number"] <= 0
        or any(
            (side.get("repo") or {}).get("full_name", "").lower() != repo
            for side in (head, base)
        )
        or base.get("ref") != policy.get("base_branch")
        or any(
            not isinstance(side.get("sha"), str) or not SHA.fullmatch(side["sha"])
            for side in (head, base)
        )
    ):
        return "unsafe_delivery_identity"
    try:
        controls.validate_pr_branch(head.get("ref"))
    except ValueError:
        return "unsafe_delivery_identity"
    return None


def _history(
    db, repo: str, pull: dict, issue: dict, policy: dict
) -> tuple[str | None, int | None]:
    from factory.orchestration.factory_intake import protected_work_history
    from factory.orchestration.factory_intake_loop import _receipt_exclusion
    from factory.orchestration.work_items import is_admissible

    rows = protected_work_history(db, repo, issue["number"])
    # Queued targets also own their branch, including before they have a task.
    all_rows = db.exec(select(FactoryReceipt).where(FactoryReceipt.repo == repo)).all()
    for row in all_rows:
        direction = json.loads(row.direction_json) if row.direction_json else {}
        branch, number = controls.granted_delivery_surface(direction)
        branch = branch or (f"factory/{row.task_id}" if row.task_id else None)
        if (
            branch == pull["head"]["ref"] or number == pull["number"]
        ) and row not in rows:
            rows.append(row)
    owner = controls.delivery_branch_owner(db, repo, pull["head"]["ref"])
    if owner:
        return "active_owner", next((r.id for r in rows if r.task_id == owner), None)
    rows.sort(key=lambda row: row.id, reverse=True)
    if any(
        row.escalation_json and json.loads(row.escalation_json).get("resolved") is None
        for row in rows
    ):
        return "escalated", rows[0].id
    if any(row.state == "queued" for row in rows):
        row = next(row for row in rows if row.state == "queued")
        return "queued_owner", row.id
    if rows:
        # An unresolved start remains authoritative even after a historical
        # receipt has left the active projection.
        unresolved = controls.unresolved_start_task_ids(db)
        if any(row.task_id in unresolved for row in rows):
            return "uncertain_execution", rows[0].id
    task_class = issue.get("_owner_task_class") or _task_class(issue)
    reason = _receipt_exclusion(
        rows,
        generation=policy["generation"],
        task_class=task_class,
        cooldown_cutoff=controls._now()
        - timedelta(hours=controls.intake_policy(policy)["cooldown_hours"]),
    )
    if reason:
        return reason, rows[0].id if rows else None
    item = db.exec(
        select(WorkItem).where(
            WorkItem.github_repo == repo,
            WorkItem.github_issue_number == issue["number"],
        )
    ).one_or_none()
    if item and item.authority == "local":
        return "local_authority", None
    if item and item.state in ("closed", "deferred", "needs_human", "done"):
        return "work_item_not_ready", None
    if item and task_class != "refine" and not is_admissible(db, item.id):
        return "work_item_not_ready", None
    return None, None


def _refined(db, repo: str, number: int) -> bool:
    """Only a server-settled brief grants direct PR delivery, never a label alone."""
    rows = db.exec(
        select(FactoryReceipt).where(
            FactoryReceipt.repo == repo,
            FactoryReceipt.issue_number == number,
            FactoryReceipt.task_class == "refine",
            FactoryReceipt.state == "succeeded",
        )
    ).all()
    task_ids = [
        row.task_id
        for row in rows
        if json.loads(row.direction_json or "{}").get("owner_pr", {}).get("direct")
    ]
    if not task_ids:
        return False
    events = db.exec(
        select(FactoryAudit).where(
            FactoryAudit.task_id.in_(task_ids),
            FactoryAudit.action == "refine_settled",
        )
    ).all()
    return any(
        json.loads(event.detail_json).get("outcome") == "agent-ready"
        for event in events
    )


def _task_class(issue: dict) -> str:
    from factory.orchestration.factory_intake_loop import (
        _label_names,
        derive_task_class,
    )

    return derive_task_class(_label_names(issue), refine=False)[0]


def _scope(
    pull: dict, issues: list[dict], repo: str, policy: dict
) -> tuple[dict | None, str | None]:
    from factory.orchestration.factory_intake_loop import _label_names

    references = closing_references(pull.get("body") or "", repo)
    if any(slug != repo for slug, _ in references):
        return None, "scope_cross_repository"
    if len(references) > 1:
        return None, "scope_ambiguous"
    matching = [issue for issue in issues if (repo, issue.get("number")) in references]
    if references and not matching:
        return None, "scope_reference_unavailable"
    if matching and "pull_request" in matching[0]:
        return matching[0], "scope_not_issue"
    # A reference is not a scope grant, but its authority/hold cannot disappear
    # merely because the bounded open-issue listing omitted that objective.
    related_refs = objective_references(pull.get("body") or "", repo) - references
    if len(related_refs) > MAX_REFERENCES:
        return None, "referenced_objective_limit"
    if any(slug != repo for slug, _ in related_refs):
        return None, "scope_cross_repository"
    related = {item["number"]: item for item in issues}
    excluded = {x.lower() for x in controls.repos_map(policy)[repo]["exclude_labels"]}
    for _, number in sorted(related_refs):
        item = related.get(number)
        if item is None:
            return None, "referenced_objective_unavailable"
        if _hold(item, excluded):
            return item, "referenced_objective_held"
    if len(matching) != 1:
        return None, "scope_authority_missing" if not matching else "scope_ambiguous"
    issue = matching[0]
    entry = controls.repos_map(policy)[repo]
    labels = _label_names(issue)
    if issue.get("state") != "open":
        return issue, "objective_not_open"
    if not labels.intersection(x.lower() for x in entry["labels"]) and issue[
        "number"
    ] not in policy.get("issue_numbers", []):
        return issue, "scope_not_accepted"
    return issue, _hold(issue, {x.lower() for x in entry["exclude_labels"]})


def candidates(
    repo: str, pulls: list[dict], issues: list[dict], policy: dict
) -> list[dict]:
    """Record every owner PR, including drafts and held work, on a due sweep."""
    from factory.orchestration.factory_intake_loop import _created_rank

    if not controls.intake_policy(policy)["owner_pr_author"]:
        return []
    from factory.orchestration import factory_conductor as conductor

    # Read missing references before taking the database control lock. Cache
    # within this bounded sweep only; selected references are fetched afresh.
    resolved, unavailable, cache = {}, {}, {}
    for pull in pulls:
        if not identity_matches(pull, policy):
            continue
        try:
            resolved[pull["number"]] = (
                _resolve_references(
                    pull, issues, repo, conductor.github_get, cache=cache
                )
                if not closing_references(pull.get("body") or "", repo)
                else issues
            )
        except (ValueError, RuntimeError, httpx.HTTPError):
            unavailable[pull["number"]] = "referenced_objective_unavailable"
    observations, result = [], []
    excluded = {x.lower() for x in controls.repos_map(policy)[repo]["exclude_labels"]}
    with controls._locked_session() as (db, _control):
        for pull in pulls:
            if not identity_matches(pull, policy):
                continue
            issue, scope_reason = _scope(
                pull, resolved.get(pull["number"], issues), repo, policy
            )
            scope_reason = unavailable.get(pull["number"], scope_reason)
            direct = scope_reason == "scope_authority_missing"
            if direct:
                from factory.orchestration.work_items import mint_or_sync_from_github

                mint_or_sync_from_github(
                    db, repo, pull, actor=controls.INTAKE_ACTOR, allow_pull_request=True
                )
                issue = {
                    **pull,
                    "_owner_task_class": "bug-fix"
                    if _refined(db, repo, pull["number"])
                    else "refine",
                }
                scope_reason = None
            task_class = (
                issue.get("_owner_task_class") or _task_class(issue) if issue else None
            )
            reason = (
                _identity_reason(pull, repo, policy)
                or _hold(pull, excluded)
                or scope_reason
                or _referenced_history(
                    db,
                    repo,
                    {n for _, n in objective_references(pull.get("body") or "", repo)}
                    - {issue["number"] if issue else None},
                )
            )
            if (
                direct
                and task_class == "refine"
                and not controls.intake_policy(policy)["refine_enabled"]
            ):
                reason = reason or "refine_disabled"
            receipt_id = None
            held = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.action == "owner_pr_discussion_held",
                    FactoryAudit.created_at >= controls._now() - timedelta(hours=24),
                )
                .order_by(FactoryAudit.id.desc())
            ).all()
            if any(
                json.loads(event.detail_json).get("repo") == repo
                and json.loads(event.detail_json).get("pr_number") == pull["number"]
                and json.loads(event.detail_json).get("pr_updated_at")
                == pull.get("updated_at")
                for event in held
            ):
                reason = reason or "explicit_discussion_hold"
            if reason is None:
                reason, receipt_id = _history(db, repo, pull, issue, policy)
            state = reason or (
                "scope_refinement"
                if task_class == "refine"
                else "draft_completion"
                if pull.get("draft")
                else "delivery_review"
            )
            observations.append(
                {
                    "pr_number": pull["number"],
                    "issue_number": issue["number"] if issue else None,
                    "created_at": pull.get("created_at"),
                    "age_seconds": max(
                        0, int(controls._now().timestamp() - _created_rank(pull))
                    )
                    if _created_rank(pull) != float("inf")
                    else None,
                    "task_id": db.get(FactoryReceipt, receipt_id).task_id
                    if receipt_id
                    else None,
                    "head_sha": (pull.get("head") or {}).get("sha"),
                    "state": state,
                    "blocker": reason,
                    "receipt_id": receipt_id,
                }
            )
            if reason is None:
                result.append(
                    {
                        "issue": issue,
                        "number": issue["number"],
                        "pr_number": pull["number"],
                        "pr_updated_at": pull.get("updated_at"),
                        "source": "owner_pr",
                        "repo": repo,
                        "work_item_id": None,
                        "direct": direct,
                        "lane": "advisory" if task_class == "refine" else "delivery",
                        "task_class": task_class,
                        "class_reason": "establish owner PR scope"
                        if task_class == "refine"
                        else "accepted owner PR objective",
                        "rank_reason": "finish existing owner PR",
                        "sort": (-1, 0, _created_rank(pull), pull["number"]),
                    }
                )
        owned = [
            row
            for row in db.exec(
                select(FactoryReceipt).where(FactoryReceipt.repo == repo)
            ).all()
            if json.loads(row.direction_json or "{}").get("owner_pr")
            and not controls.is_advisory(row.task_class)
        ]
        task_ids = [row.task_id for row in owned if row.task_id]
        merged = (
            set(
                db.exec(
                    select(FactoryAudit.task_id).where(
                        FactoryAudit.task_id.in_(task_ids),
                        FactoryAudit.action.in_(("merged", "owned_pr_merged")),
                    )
                ).all()
            )
            if task_ids
            else set()
        )
        controls._audit(
            db,
            controls.INTAKE_ACTOR,
            "owner_pr_backlog",
            repo=repo,
            items=observations,
            merged_deliveries=len(merged),
            verified_deliveries=sum(row.state == "succeeded" for row in owned),
            counts={
                state: sum(x["state"] == state for x in observations)
                for state in sorted({x["state"] for x in observations})
            },
        )
    return result


def receive(repo: str, number: int, policy: dict, *, generation: int) -> dict:
    """Recheck chosen PR, objective and discussion before the atomic branch grant."""
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.dependency_prs import _pages
    from factory.orchestration.factory_intake import receive_issue

    pull = conductor.github_get(repo, f"pulls/{number}")
    reason = _identity_reason(pull, repo, policy)
    if reason:
        raise ValueError(reason)
    # Resolve only the already selected canonical objective, never scan or infer
    # a different objective after selection.
    expected_issue = policy["owner_pr_issue_number"]
    issue = conductor.github_get(repo, f"issues/{expected_issue}")
    direct = expected_issue == number
    if direct and closing_references(pull.get("body") or "", repo):
        raise ValueError("scope_changed")
    related = _resolve_references(pull, [issue], repo, conductor.github_get)
    scoped, reason = _scope(pull, related, repo, policy)
    if direct and reason == "scope_authority_missing":
        scoped, reason = issue, None
    excluded = {x.lower() for x in controls.repos_map(policy)[repo]["exclude_labels"]}
    reason = reason or _hold(pull, excluded) or _hold(issue, excluded)
    if reason or scoped is None or issue.get("number") != expected_issue:
        raise ValueError(reason or "scope_changed")
    reference_numbers = {
        n for _, n in objective_references(pull.get("body") or "", repo)
    }
    for resource in (
        f"issues/{n}/comments"
        for n in sorted(reference_numbers | {number, expected_issue})
    ):
        comments = _pages(repo, resource, conductor.github_list)
        if any(has_hold(comment.get("body") or "") for comment in comments):
            raise OwnerPRHold("explicit_discussion_hold")
    # Evidence was fetched outside the control lock; all competing intake and
    # branch grants serialize with this final policy/history check.
    with controls._locked_session() as (db, control):
        live = json.loads(control.policy_json)
        entry = controls.repos_map(live).get(repo, {})
        if (
            control.state != "enabled"
            or live.get("generation") != generation
            or not controls.intake_policy(live)["enabled"]
            or controls.intake_policy(live)["refinement_batch"]
            or not entry.get("enabled")
            or entry.get("paused")
            or not identity_matches(pull, live)
        ):
            raise ValueError("owner_pr_intake_authority_changed")
        if _referenced_history(db, repo, reference_numbers - {expected_issue}):
            raise ValueError("referenced_objective_held")
        if direct:
            issue = {
                **issue,
                "_owner_task_class": "bug-fix"
                if _refined(db, repo, number)
                else "refine",
            }
            scoped, reason = issue, None
            if (
                issue["_owner_task_class"] == "refine"
                and not controls.intake_policy(live)["refine_enabled"]
            ):
                raise ValueError("refine_disabled")
        else:
            scoped, reason = _scope(pull, related, repo, live)
        if reason or _hold(pull, {x.lower() for x in entry["exclude_labels"]}):
            raise ValueError(reason or "explicit_hold")
        reason, owner = _history(db, repo, pull, issue, live)
        if reason:
            if reason == "queued_owner":
                return {"ok": True, "created": False, "receipt": {"id": owner}}
            raise ValueError(reason)
        if direct:
            from factory.orchestration.work_items import mint_or_sync_from_github

            item, _ = mint_or_sync_from_github(
                db, repo, issue, actor=controls.INTAKE_ACTOR, allow_pull_request=True
            )
            row = FactoryReceipt(
                repo=repo,
                issue_number=number,
                generation=generation,
                title=issue["title"],
                body=issue.get("body") or "",
                url=f"https://github.com/{repo}/pull/{number}",
                actor=controls.INTAKE_ACTOR,
                task_class=issue["_owner_task_class"],
                work_item_id=item.id,
            )
            db.add(row)
            db.flush()
            received = {"ok": True, "created": True, "receipt": {"id": row.id}}
        else:
            received = receive_issue(
                repo,
                expected_issue,
                issue["title"],
                issue.get("body") or "",
                issue["html_url"],
                controls.INTAKE_ACTOR,
                generation=generation,
                task_class=_task_class(issue),
                issue=issue,
                session=db,
            )
            row = db.get(FactoryReceipt, received["receipt"]["id"])
        if not received["created"]:
            return received
        direction = {
            "delivery_branch": pull["head"]["ref"],
            "delivery_pr_number": number,
            "delivery_adoption": True,
            "delivery_target_checked": True,
            "owner_pr": {
                "author_id": pull["user"]["id"],
                "observed_head_sha": pull["head"]["sha"],
                "direct": direct,
            },
        }
        row.direction_json = json.dumps(direction)
        controls._audit(
            db,
            controls.INTAKE_ACTOR,
            "receive_owner_pr",
            receipt_id=row.id,
            repo=repo,
            pr_number=number,
            issue_number=expected_issue,
            head_sha=pull["head"]["sha"],
        )
        return received


def assert_unheld(
    task: dict, pull: dict | None = None, *, read_get=None, read_list=None
) -> None:
    """Fresh PR holds fence new starts and landing without settling live work."""
    if not task.get("owner_pr"):
        return
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.dependency_prs import _pages

    read_get, read_list = (
        read_get or conductor.github_get,
        read_list or conductor.github_list,
    )
    number = task["delivery_pr_number"]
    pull = pull or read_get(task["repo"], f"pulls/{number}")
    if pull.get("user", {}).get("id") != task["owner_pr"]["author_id"]:
        raise ValueError("owner_pr_author_changed")
    if _hold(pull, set()):
        raise OwnerPRHold("owner_pr_explicit_hold")
    comments = _pages(task["repo"], f"issues/{number}/comments", read_list)
    if any(has_hold(comment.get("body") or "") for comment in comments):
        raise OwnerPRHold("owner_pr_explicit_discussion_hold")
    if pull.get("state") != "open" and not pull.get("merged"):
        raise ValueError("owner_pr_closed_without_merge")
