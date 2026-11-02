"""Authenticated operator controls, independent of conductor responsiveness."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from auth.api import Principal
from factory.access import operator
from goosecracker.api import REPO_CATALOG
from factory.orchestration.recovery_delegation import (
    Request as RecoveryAuthorizationRequest,
)

router = APIRouter(prefix="/api/swarm/factory", tags=["factory"])


@router.post("/recovery-authorizations")
async def factory_recovery_authorization(
    body: RecoveryAuthorizationRequest, principal: Principal = Depends(operator)
) -> dict:
    from factory.orchestration.recovery_delegation import request_authorization

    try:
        return await request_authorization(principal, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/recovery-authorizations/{grant_id}")
def factory_recovery_authorization_status(
    grant_id: int, principal: Principal = Depends(operator)
) -> dict:
    from factory.orchestration.recovery_delegation import status

    try:
        return status(grant_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class RecoveryRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.post("/recovery-authorizations/{grant_id}/revoke")
def factory_recovery_revoke(
    grant_id: int, body: RecoveryRevokeRequest, principal: Principal = Depends(operator)
) -> dict:
    from factory.orchestration.recovery_delegation import revoke

    try:
        return revoke(principal, grant_id, body.expected_preview_sha256)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    task_id: str | None = None
    repo: str | None = None
    policy: dict | None = None
    node_key: str | None = None
    attempt: int | None = Field(default=None, ge=1, strict=True)
    session_id: int | None = Field(default=None, gt=0, strict=True)
    request_key: str | None = Field(default=None, min_length=1, max_length=256)
    expected_version: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    expected_identity_sha256: str | None = None
    reason: str | None = None


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    issue_number: int = Field(gt=0)
    generation: int = Field(default=0, ge=0)


class SyntheticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    chart_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    image: str = Field(min_length=1, max_length=256)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    promotion: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")


@router.post("/synthetics")
def factory_synthetics(
    body: SyntheticRequest, principal: Principal = Depends(operator)
) -> dict:
    """Manual, default-off fixture reads. Never admits work or rolls back."""
    import os

    from sqlalchemy.exc import SQLAlchemyError

    from core.db import get_engine
    from factory.orchestration.synthetic_actions import Inconclusive, verify

    if os.environ.get("FACTORY_SYNTHETICS_ENABLED") != "true":
        raise HTTPException(404, "factory synthetics are disabled")
    try:
        return verify(get_engine(), **body.model_dump())
    except (Inconclusive, SQLAlchemyError, ValueError) as exc:
        # Never return SQL, parameters, fixture bodies or connection details.
        return {
            "verdict": "inconclusive",
            "reason": str(exc) if isinstance(exc, Inconclusive) else type(exc).__name__,
        }


class DecisionRequest(BaseModel):
    """One operator answer to one escalation: pick an option, or ask for more."""

    model_config = ConfigDict(extra="forbid")
    option_key: str | None = Field(default=None, max_length=32)
    decision_id: str = Field(pattern=r"^decision:[0-9a-f]{64}$")
    request_key: str = Field(min_length=1, max_length=256)
    action: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=4000)


def _raise_refused(result: dict) -> None:
    """Preserve HTTP refusal status without discarding the durable outcome."""
    if not result.get("ok") and result.get("state") == "refused":
        raise HTTPException(
            int(result.get("status") or 409),
            result.get("reason") or "factory request refused",
        )


@router.get("")
def factory_status(principal: Principal = Depends(operator)) -> dict:
    from factory.orchestration import graph
    from factory.orchestration.factory_controls import status

    result = status()
    for receipt in result["receipts"]:
        if receipt["task_id"]:
            receipt["graph"] = graph.load_graph(receipt["task_id"])
            receipt["node_runs"] = graph.node_runs(receipt["task_id"])
    return result


@router.post("/control")
def factory_control(
    body: ControlRequest, principal: Principal = Depends(operator)
) -> dict:
    from factory.orchestration.factory_controls import request_control, set_control

    attempt_fields = (
        body.node_key,
        body.attempt,
        body.session_id,
        body.expected_identity_sha256,
        body.reason,
    )
    if body.action == "stop_attempt":
        from factory.orchestration.factory_attempt_stop import request_attempt_stop

        if (
            body.task_id is None
            or body.policy is not None
            or body.request_key is None
            or body.expected_version is not None
            or any(value is None for value in attempt_fields)
        ):
            raise HTTPException(422, "exact attempt stop fields are required")
        try:
            return request_attempt_stop(
                task_id=body.task_id,
                node_key=body.node_key,
                attempt=body.attempt,
                session_id=body.session_id,
                request_key=body.request_key,
                expected_identity_sha256=body.expected_identity_sha256,
                reason=body.reason,
                actor=principal.subject,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if any(value is not None for value in attempt_fields):
        raise HTTPException(422, "attempt fields require stop_attempt")
    if body.policy is not None:
        extra = body.policy.get("repos") or {}
        posted = [
            body.policy.get("repo"),
            *(extra.keys() if isinstance(extra, dict) else []),
        ]
        if any(
            not isinstance(name, str) or name.lower() not in REPO_CATALOG
            for name in posted
        ):
            raise HTTPException(422, "repository is not available to the executor")
    try:
        from factory.orchestration.factory_controls import REPO_ACTIONS

        if body.action in REPO_ACTIONS:
            if (
                body.policy is not None
                or body.task_id is not None
                or body.repo is None
                or body.request_key is not None
                or body.expected_version is not None
                or any(value is not None for value in attempt_fields)
            ):
                raise HTTPException(422, "repo actions take only a repo")
            if body.repo.lower() not in REPO_CATALOG:
                raise HTTPException(422, "repository is not available to the executor")
            result = set_control(body.action, principal.subject, repo=body.repo)
            if not result["ok"]:
                raise HTTPException(409, result)
            return result
        if body.action == "configure":
            if body.request_key is not None or body.expected_version is not None:
                raise HTTPException(
                    422, "configure does not accept a control request identity"
                )
            if body.repo is not None:
                raise HTTPException(422, "configure takes a policy, not a repo")
            result = set_control(
                body.action,
                principal.subject,
                policy=body.policy,
                task_id=body.task_id,
            )
        else:
            if body.policy is not None:
                raise HTTPException(422, "policy requires configure action")
            if body.repo is not None:
                raise HTTPException(422, "repo requires a per-repo action")
            if body.request_key is None or body.expected_version is None:
                raise HTTPException(
                    422, "request_key and expected_version are required"
                )
            result = request_control(
                body.action,
                principal.subject,
                request_key=body.request_key,
                expected_version=body.expected_version,
                task_id=body.task_id,
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not result["ok"]:
        raise HTTPException(409, result)
    # A persisted stop means admission is fenced. Active descendants are
    # separately reported as unconfirmed until cancellation is reconciled.
    return result


@router.post("/decisions/{receipt_id}")
def factory_decision(
    receipt_id: int,
    body: DecisionRequest,
    principal: Principal = Depends(operator),
) -> dict:
    """Answer one escalation. Either pick an option, or ask for another brief.

    The same operator gate as /control, because an option applies labels,
    comments, child issues and closes to the repository under the monolith's
    own credential. A decision is a write, not a view.
    """
    from factory.orchestration.factory_decisions import request_decision

    chat = body.action == "chat"
    if body.action is not None and not chat:
        raise HTTPException(422, "the only supported action is chat")
    if chat == bool(body.option_key):
        raise HTTPException(422, "supply exactly one of option_key or action=chat")
    try:
        result = request_decision(
            receipt_id,
            body.decision_id,
            "chat" if chat else body.option_key or "",
            principal.subject,
            request_key=body.request_key,
            note=body.note,
            action="chat" if chat else "decide",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _raise_refused(result)
    from factory.orchestration.conductor_context import with_request_knowledge

    return with_request_knowledge(result, principal.subject, body.request_key)


def factory_receipt(body: ReceiptRequest, principal: Principal) -> dict:
    """Validate and receive MCP issue submissions, without an HTTP registration."""
    from factory.orchestration.factory_conductor import github_get
    from factory.orchestration.factory_intake import receive_issue

    if body.repo not in REPO_CATALOG:
        raise HTTPException(422, "repository is not available to the executor")
    issue = github_get(body.repo, f"issues/{body.issue_number}")
    if issue.get("state") != "open" or "pull_request" in issue:
        raise HTTPException(409, "issue is not open eligible work")
    try:
        from factory.orchestration.factory_gates import receive_delivery_target

        delivery_target = receive_delivery_target(body.repo, body.issue_number)
        return receive_issue(
            body.repo,
            body.issue_number,
            issue["title"],
            issue.get("body") or "",
            issue["html_url"],
            principal.subject,
            generation=body.generation,
            issue=issue,
            delivery_target=delivery_target,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class ImprovementRequest(BaseModel):
    """A bounded scouting batch after reviewing exact current context."""

    model_config = ConfigDict(extra="forbid")
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules_reviewed: bool
    dedup_reviewed: bool
    subscription_caps: dict
    findings: list[dict] = Field(max_length=20)


@router.get("/improvements/context")
def improvement_context(principal: Principal = Depends(operator)) -> dict:
    from factory.orchestration.factory_improvements import context

    try:
        return context()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/improvements")
def improvement_submit(
    body: ImprovementRequest, principal: Principal = Depends(operator)
) -> dict:
    from factory.orchestration.factory_improvements import submit

    try:
        return submit(body.model_dump(), actor=principal.subject)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
