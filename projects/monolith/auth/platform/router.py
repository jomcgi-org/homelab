"""Private management transport and capability-scoped enrollment callbacks."""

from typing import Annotated, Literal

from core.db import get_engine, get_session
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlmodel import Session
from starlette.responses import JSONResponse

from auth.api import Authority, Principal, PrincipalKind
from auth.platform import service
from auth.platform.enrollment import acknowledge_activation, activate, receipt_verifier
from auth.platform.identity import browser_or_operator


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                service.gate(
                    "enrollment" if "/enrollment/" in request.url.path else "management"
                )
                response = await handler(request)
            except RequestValidationError:
                # FastAPI's default response echoes invalid inputs, including
                # enrollment capabilities. Never echo request bodies here.
                response = JSONResponse(
                    {"detail": "Invalid platform request."}, status_code=422
                )
            except HTTPException as error:
                response = JSONResponse(
                    {"detail": error.detail}, status_code=error.status_code
                )
            private_response(response)
            return response

        return safe


router = APIRouter(
    prefix="/api/auth/platform", tags=["platform-auth"], route_class=PrivateRoute
)


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "bootstrap", "issue", "revoke_invitation", "set_active", "grant", "revoke_grant"
    ]
    request_id: str = Field(min_length=8, max_length=96)
    reason: str = Field(min_length=1, max_length=500)
    arguments: dict = Field(default_factory=dict, max_length=3)

    @model_validator(mode="after")
    def command_fields(self):
        if {
            "session",
            "principal",
            "action",
            "request_id",
            "reason",
        } & self.arguments.keys():
            raise ValueError("Reserved command field")
        return self


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=8, max_length=96)
    reason: str = Field(min_length=1, max_length=500)
    reissue: bool = False


class Capability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=43, max_length=43)


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt: SecretStr = Field(min_length=1, max_length=8192)


@router.post("/commands")
def mutate(
    body: Mutation,
    response: Response,
    principal: Annotated[Principal, Depends(browser_or_operator)],
    session: Annotated[Session, Depends(get_session)],
):
    private_response(response)
    return service.command(
        session,
        principal,
        body.action,
        request_id=body.request_id,
        reason=body.reason,
        **body.arguments,
    )


@router.post("/invitations/{invitation_id}/deliver")
def deliver(
    invitation_id: str,
    body: Delivery,
    response: Response,
    principal: Annotated[Principal, Depends(browser_or_operator)],
    session: Annotated[Session, Depends(get_session)],
):
    private_response(response)
    return service.command(
        session,
        principal,
        "deliver",
        request_id=body.request_id,
        reason=body.reason,
        invitation_id=invitation_id,
        reissue=body.reissue,
    )


@router.get("/management/{kind}")
def directory(
    kind: Literal["users", "invitations", "permissions", "user", "invitation"],
    response: Response,
    principal: Annotated[Principal, Depends(browser_or_operator)],
    session: Annotated[Session, Depends(get_session)],
    user_id: str | None = None,
    invitation_id: str | None = None,
    limit: int = 50,
    after: str | None = None,
):
    private_response(response)
    return service.read(
        session,
        principal,
        kind,
        user_id=user_id,
        invitation_id=invitation_id,
        limit=limit,
        after=after,
    )


@router.get("/self")
def own_profile(
    response: Response,
    principal: Annotated[Principal, Depends(browser_or_operator)],
    session: Annotated[Session, Depends(get_session)],
):
    service.gate()
    private_response(response)
    if (
        principal.authority is not Authority.STANDING
        or principal.kind is not PrincipalKind.HUMAN
        or principal.user_type not in ("internal", "external")
        or principal.delegation_claim_present
        or principal.actor
    ):
        raise HTTPException(403, "Standing human authorization required.")
    user = service.identity_user(session, principal)
    if user is None or not user.active:
        raise HTTPException(403, "Active platform account required.")
    return service.user_view(session, user)


@router.post("/enrollment/validate")
def validate(
    body: Capability,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
):
    private_response(response)
    row = service.inspect_invitation(
        session, body.token.get_secret_value(), allow_completed=True
    )
    return {
        "invitation_id": row.id,
        "expires_at": service.utc(row.expires_at).isoformat(),
        "status": row.status,
        "accepted_subject": row.accepted_subject,
        "activation_pending": not row.identity_activated,
    }


def _activate(completion):
    with Session(get_engine()) as session:
        return activate(session, completion)


@router.post("/enrollment/complete")
async def complete(body: Receipt, response: Response):
    import asyncio

    private_response(response)
    completion = await receipt_verifier().verify(body.receipt.get_secret_value())
    return await asyncio.to_thread(_activate, completion)


def _acknowledge(completion):
    with Session(get_engine()) as session:
        return acknowledge_activation(session, completion)


@router.post("/enrollment/acknowledge")
async def acknowledge(body: Receipt, response: Response):
    import asyncio

    private_response(response)
    completion = await receipt_verifier().verify(
        body.receipt.get_secret_value(), phase="activated"
    )
    return await asyncio.to_thread(_acknowledge, completion)
