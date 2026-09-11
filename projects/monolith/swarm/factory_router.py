"""Authenticated operator controls, independent of conductor responsiveness."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from auth.api import Authority, Principal, PrincipalKind, get_principal
from goosecracker.api import REPO_CATALOG

router = APIRouter(prefix="/api/swarm/factory", tags=["factory"])


def operator(principal: Principal = Depends(get_principal)) -> Principal:
    if (
        principal.authority != Authority.STANDING
        or principal.kind != PrincipalKind.HUMAN
        or not principal.has_group("operators")
    ):
        raise HTTPException(403, "standing operator authority is required")
    return principal


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    task_id: str | None = None
    policy: dict | None = None
    node_key: str | None = None
    attempt: int | None = Field(default=None, ge=1, strict=True)
    session_id: int | None = Field(default=None, gt=0, strict=True)
    request_key: str | None = None
    expected_identity_sha256: str | None = None
    reason: str | None = None


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    issue_number: int = Field(gt=0)
    generation: int = Field(default=0, ge=0)


class DecisionRequest(BaseModel):
    """One operator answer to one escalation: pick an option, or ask for more."""

    model_config = ConfigDict(extra="forbid")
    option_key: str | None = Field(default=None, max_length=32)
    action: str | None = None
    note: str | None = Field(default=None, max_length=4000)


@router.get("")
def factory_status(principal: Principal = Depends(operator)) -> dict:
    from swarm import graph
    from swarm.factory_controls import status

    result = status()
    for receipt in result["receipts"]:
        if receipt["task_id"]:
            receipt["graph"] = graph.load_graph(receipt["task_id"])
            receipt["node_runs"] = graph.node_runs(receipt["task_id"])
    return result


@router.get("/attempt-stop")
def attempt_stop_preview(
    task_id: str,
    node_key: str,
    attempt: int,
    session_id: int,
    principal: Principal = Depends(operator),
) -> dict:
    from swarm.factory_attempt_stop import read_attempt_stop

    try:
        return read_attempt_stop(task_id, node_key, attempt, session_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/control")
def factory_control(
    body: ControlRequest, principal: Principal = Depends(operator)
) -> dict:
    from swarm.factory_controls import set_control

    attempt_fields = (
        body.node_key,
        body.attempt,
        body.session_id,
        body.request_key,
        body.expected_identity_sha256,
        body.reason,
    )
    if body.action == "stop_attempt":
        from swarm.factory_attempt_stop import request_attempt_stop

        if (
            body.task_id is None
            or body.policy is not None
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
    if body.policy is not None and body.policy.get("repo") not in REPO_CATALOG:
        raise HTTPException(422, "repository is not available to the executor")
    try:
        result = set_control(
            body.action, principal.subject, policy=body.policy, task_id=body.task_id
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not result["ok"]:
        raise HTTPException(409, result)
    # A persisted stop means admission is fenced. Active descendants are
    # separately reported as unconfirmed until cancellation is reconciled.
    return result


@router.get("/escalations")
def factory_escalations(principal: Principal = Depends(operator)) -> dict:
    """Every escalation the lane has raised, open ones first."""
    from swarm.factory_controls import escalations, status

    state = status()
    if not state.get("ok"):
        return {"ok": False, "reason": state.get("reason"), "escalations": []}
    return {"ok": True, "escalations": escalations(state["receipts"])}


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
    from swarm.factory_decisions import DecisionError, apply_decision, request_chat

    chat = body.action == "chat"
    if body.action is not None and not chat:
        raise HTTPException(422, "the only supported action is chat")
    if chat == bool(body.option_key):
        raise HTTPException(422, "supply exactly one of option_key or action=chat")
    try:
        if chat:
            if not (body.note or "").strip():
                raise HTTPException(422, "a chat request needs a note")
            return request_chat(receipt_id, body.note or "", principal.subject)
        return apply_decision(
            receipt_id, body.option_key or "", principal.subject, body.note
        )
    except DecisionError as exc:
        raise HTTPException(exc.status, exc.reason) from exc


@router.post("/issues")
def factory_receipt(
    body: ReceiptRequest, principal: Principal = Depends(operator)
) -> dict:
    from swarm.factory_conductor import github_get
    from swarm.factory_intake import receive_issue

    if body.repo not in REPO_CATALOG:
        raise HTTPException(422, "repository is not available to the executor")
    issue = github_get(body.repo, f"issues/{body.issue_number}")
    if issue.get("state") != "open" or "pull_request" in issue:
        raise HTTPException(409, "issue is not open eligible work")
    try:
        return receive_issue(
            body.repo,
            body.issue_number,
            issue["title"],
            issue.get("body") or "",
            issue["html_url"],
            principal.subject,
            generation=body.generation,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
