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


class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    issue_number: int = Field(gt=0)
    generation: int = Field(default=0, ge=0)


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


@router.post("/control")
def factory_control(
    body: ControlRequest, principal: Principal = Depends(operator)
) -> dict:
    from swarm.factory_controls import set_control

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
