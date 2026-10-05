"""Commands shared by HTTP and MCP; all authority checks live here."""

import hashlib
import json
import os
import re
import secrets
from datetime import timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from auth.api import Authority, Principal, PrincipalKind
from auth.platform.models import (
    PlatformApplicationUser,
    PlatformAudit,
    PlatformCommand,
    PlatformGrant,
    PlatformIdentity,
    PlatformInvitation,
    PlatformUser,
    now,
)

PERMISSIONS = {
    "grimoire.access": "Enter Grimoire; campaign membership is checked separately.",
    "grimoire.create_game": "Create a campaign; does not grant access to other campaigns.",
}


def enabled(name="management"):
    return os.getenv(f"PLATFORM_AUTH_{name.upper()}_ENABLED", "") == "true"


def gate(name="management"):
    if not enabled(name):
        raise HTTPException(404, "Platform authentication feature is not enabled.")


def utc(value):
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def actor_key(principal):
    return hashlib.sha256(
        json.dumps([principal.issuer, principal.subject]).encode()
    ).hexdigest()


def identity_user(session, principal, *, lock=False):
    query = (
        select(PlatformUser)
        .join(PlatformIdentity)
        .where(
            PlatformIdentity.issuer == principal.issuer,
            PlatformIdentity.subject == principal.subject,
        )
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.exec(query).first()


def require_operator(session, principal, *, bootstrap=False):
    gate()
    human_issuers = {
        os.getenv("AUTH_AUTHENTIK_ISSUER", ""),
        os.getenv("PLATFORM_AUTH_LOGIN_ISSUER", ""),
    } - {""}
    if (
        principal.authority is not Authority.STANDING
        or principal.kind is not PrincipalKind.HUMAN
        or not principal.issuer
        or not principal.subject
        or not principal.email
        or principal.user_type not in ("internal", "external")
        or principal.delegation_claim_present
        or principal.issuer not in human_issuers
        or principal.actor
        or not principal.has_group("operators")
    ):
        raise HTTPException(403, "Standing human operator authorization required.")
    user = identity_user(session, principal, lock=True)
    if user is not None and not user.active:
        raise HTTPException(403, "Platform account is disabled.")
    if user is None and not bootstrap:
        raise HTTPException(403, "Explicit platform operator bootstrap required.")
    return user


def require_permission(session, principal, permission):
    if permission not in PERMISSIONS:
        raise HTTPException(403, "Unknown application permission.")
    user = identity_user(session, principal)
    if (
        principal.authority is not Authority.STANDING
        or principal.kind is not PrincipalKind.HUMAN
        or principal.user_type not in ("internal", "external")
        or principal.delegation_claim_present
        or user is None
        or not user.active
    ):
        raise HTTPException(403, "Active platform account required.")
    grant = session.exec(
        select(PlatformGrant).where(
            PlatformGrant.user_id == user.id,
            PlatformGrant.permission == permission,
        )
    ).first()
    if grant is None:
        raise HTTPException(403, "Application permission required.")
    return user


def bind_application_user(session, principal, application, application_user_id):
    """Bind the app's stable ID in its transaction, without replacing memberships."""
    if application != "grimoire":
        raise HTTPException(400, "Unknown platform application.")
    user = require_permission(session, principal, "grimoire.access")
    app_id = identifier(application_user_id)
    existing = session.exec(
        select(PlatformApplicationUser)
        .where(
            PlatformApplicationUser.application == application,
            PlatformApplicationUser.user_id == user.id,
        )
        .with_for_update()
    ).first()
    if existing is not None:
        if existing.application_user_id != app_id:
            raise HTTPException(
                409, "Application identity is already linked to another account."
            )
        return existing
    row = PlatformApplicationUser(
        user_id=user.id, application=application, application_user_id=app_id
    )
    session.add(row)
    return row


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid platform identifier.") from None


def email_address(value):
    value = str(value).strip().lower()
    if len(value) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(400, "Enter a valid email address.")
    return value


def request_fields(request_id, reason):
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_.:-]{8,96}", request_id
    ):
        raise HTTPException(400, "A bounded idempotency key is required.")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise HTTPException(400, "A reason is required.")


def user_view(session, user):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "active": user.active,
        "identities": [
            {"issuer": i.issuer, "subject": i.subject}
            for i in session.exec(
                select(PlatformIdentity).where(PlatformIdentity.user_id == user.id)
            ).all()
        ],
        "permissions": sorted(
            g.permission
            for g in session.exec(
                select(PlatformGrant).where(PlatformGrant.user_id == user.id)
            ).all()
        ),
    }


def invitation_view(row):
    return {
        "id": row.id,
        "recipient_label": row.recipient_label,
        "status": "expired"
        if row.status in ("awaiting_delivery", "pending")
        and utc(row.expires_at) <= now()
        else row.status,
        "expires_at": utc(row.expires_at).isoformat(),
        "delivery_reference": f"/grimoire/platform?invitation_id={row.id}",
    }


def audit(session, principal, action, target, request_id, reason):
    row = PlatformAudit(
        issuer=principal.issuer,
        subject=principal.subject,
        action=action,
        target=target,
        request_id=request_id,
        reason=reason.strip(),
    )
    session.add(row)
    return row


def target_user(session, value):
    row = session.exec(
        select(PlatformUser)
        .where(PlatformUser.id == identifier(value))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise HTTPException(404, "Platform user not found.")
    return row


def target_invitation(session, value):
    row = session.exec(
        select(PlatformInvitation)
        .where(PlatformInvitation.id == identifier(value))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise HTTPException(404, "Platform invitation not found.")
    return row


def command(
    session: Session,
    principal: Principal,
    action: str,
    *,
    request_id: str,
    reason: str,
    **arguments,
):
    try:
        return _command(
            session,
            principal,
            action,
            request_id=request_id,
            reason=reason,
            **arguments,
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            409, "Platform state changed. Retry with the same command key."
        ) from None


def _command(
    session: Session,
    principal: Principal,
    action: str,
    *,
    request_id: str,
    reason: str,
    **arguments,
):
    """Apply a bounded management mutation and its audit in one transaction."""
    operator = require_operator(session, principal, bootstrap=action == "bootstrap")
    request_fields(request_id, reason)
    actor = actor_key(principal)
    fingerprint = hashlib.sha256(
        json.dumps([action, reason, arguments], sort_keys=True).encode()
    ).hexdigest()
    previous = session.exec(
        select(PlatformCommand).where(
            PlatformCommand.actor == actor, PlatformCommand.request_id == request_id
        )
    ).first()
    if previous:
        if previous.fingerprint != fingerprint:
            raise HTTPException(
                409, "Idempotency key already used for another command."
            )
        if action == "deliver":
            raise HTTPException(
                409, "Link already delivered. Authorize a reissue if delivery was lost."
            )
        return json.loads(previous.result_json)
    raw_token = None
    if action == "bootstrap":
        if set(arguments) - {"user_id"}:
            raise HTTPException(
                400, "Bootstrap only imports the authenticated operator."
            )
        target_id = arguments.get("user_id")
        if target_id and operator is not None and operator.id != identifier(target_id):
            raise HTTPException(
                409, "Operator identity is already linked to another platform user."
            )
        if operator is None:
            if target_id:
                operator = target_user(session, target_id)
                if not operator.active:
                    raise HTTPException(
                        403, "Cannot bootstrap a disabled platform account."
                    )
                issuers = {
                    os.getenv("AUTH_AUTHENTIK_ISSUER", ""),
                    os.getenv("PLATFORM_AUTH_LOGIN_ISSUER", ""),
                } - {""}
                same_person = session.exec(
                    select(PlatformIdentity).where(
                        PlatformIdentity.user_id == operator.id,
                        PlatformIdentity.issuer.in_(issuers),
                        PlatformIdentity.subject == principal.subject,
                    )
                ).first()
                if same_person is None:
                    raise HTTPException(
                        403,
                        "Only this operator's matching stable Authentik subject can be linked.",
                    )
            else:
                operator = PlatformUser(
                    username=principal.username or f"operator-{actor[:16]}",
                    email=email_address(principal.email),
                    display_name=(principal.display_name or principal.email)[:200],
                )
                session.add(operator)
                session.flush()
            session.add(
                PlatformIdentity(
                    user_id=operator.id,
                    issuer=principal.issuer,
                    subject=principal.subject,
                )
            )
            session.flush()
        result = user_view(session, operator)
        target = operator.id
    elif action == "issue":
        if set(arguments) != {"recipient_label", "expires_in_days"}:
            raise HTTPException(400, "Invalid invitation fields.")
        days = arguments["expires_in_days"]
        if type(days) is not int or not 1 <= days <= 7:
            raise HTTPException(400, "Invitation expiry must be one to seven days.")
        label = arguments["recipient_label"]
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
            raise HTTPException(400, "A recipient label is required.")
        row = PlatformInvitation(
            recipient_label=label.strip(),
            issued_by=actor,
            expires_at=now() + timedelta(days=days),
        )
        session.add(row)
        result, target = invitation_view(row), row.id
    elif action == "revoke_invitation":
        if set(arguments) != {"invitation_id"}:
            raise HTTPException(400, "Invalid invitation fields.")
        row = target_invitation(session, arguments["invitation_id"])
        if row.status == "accepted":
            raise HTTPException(
                409, "Account already enrolled. Disable the platform user instead."
            )
        row.status = "revoked"
        row.token_digest = None
        result, target = invitation_view(row), row.id
    elif action == "deliver":
        if (
            set(arguments) != {"invitation_id", "reissue"}
            or type(arguments["reissue"]) is not bool
        ):
            raise HTTPException(400, "Invalid delivery fields.")
        row = target_invitation(session, arguments["invitation_id"])
        if utc(row.expires_at) <= now() or row.status not in (
            "awaiting_delivery",
            "pending",
        ):
            raise HTTPException(410, "Invitation expired, revoked or accepted.")
        if row.status == "pending" and not arguments["reissue"]:
            raise HTTPException(
                409, "Link already delivered. Authorize a reissue if delivery was lost."
            )
        raw_token = secrets.token_urlsafe(32)
        row.token_digest = hashlib.sha256(raw_token.encode()).hexdigest()
        row.status = "pending"
        result, target = invitation_view(row), row.id
    elif action in ("set_active", "grant", "revoke_grant"):
        expected = (
            {"user_id", "active"}
            if action == "set_active"
            else {"user_id", "permission"}
        )
        if set(arguments) != expected:
            raise HTTPException(400, "Invalid user-management fields.")
        row = target_user(session, arguments["user_id"])
        if action == "set_active":
            if type(arguments["active"]) is not bool:
                raise HTTPException(400, "Active must be a boolean.")
            row.active = arguments["active"]
        else:
            permission = arguments["permission"]
            if not isinstance(permission, str) or permission not in PERMISSIONS:
                raise HTTPException(400, "Unknown application permission.")
            existing = session.exec(
                select(PlatformGrant).where(
                    PlatformGrant.user_id == row.id,
                    PlatformGrant.permission == permission,
                )
            ).first()
            if action == "grant" and existing is None:
                session.add(
                    PlatformGrant(
                        user_id=row.id, permission=permission, issued_by=actor
                    )
                )
            elif action == "revoke_grant" and existing is not None:
                session.delete(existing)
        session.flush()
        result, target = user_view(session, row), row.id
    else:
        raise HTTPException(400, "Unknown platform command.")
    record = audit(session, principal, action, target, request_id, reason)
    result = {**result, "audit_reference": record.id}
    session.add(
        PlatformCommand(
            actor=actor,
            request_id=request_id,
            fingerprint=fingerprint,
            result_json=json.dumps(result, sort_keys=True),
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            409, "Platform state changed. Retry with the same command key."
        ) from None
    return {**result, "token": raw_token} if raw_token is not None else result


def read(
    session, principal, kind, *, user_id=None, invitation_id=None, limit=50, after=None
):
    require_operator(session, principal)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HTTPException(400, "Page size must be one to 100.")
    if kind == "permissions":
        return [{"name": k, "description": v} for k, v in PERMISSIONS.items()]
    if kind == "user":
        return user_view(session, target_user(session, user_id))
    if kind == "invitation":
        return invitation_view(target_invitation(session, invitation_id))
    model = (
        PlatformUser
        if kind == "users"
        else PlatformInvitation
        if kind == "invitations"
        else None
    )
    if model is None:
        raise HTTPException(400, "Unknown management query.")
    query = select(model).order_by(model.id).limit(limit + 1)
    if after is not None:
        query = query.where(model.id > identifier(after))
    rows = session.exec(query).all()
    view = (
        (lambda row: user_view(session, row))
        if model is PlatformUser
        else invitation_view
    )
    return {
        "items": [view(row) for row in rows[:limit]],
        "next": rows[limit - 1].id if len(rows) > limit else None,
    }


def inspect_invitation(session, token, *, allow_completed=False):
    gate("enrollment")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(404, "Platform invitation unavailable.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = session.exec(
        select(PlatformInvitation)
        .where(PlatformInvitation.token_digest == digest)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        row is None
        or row.status
        not in (("pending", "accepted") if allow_completed else ("pending",))
        or utc(row.expires_at) <= now()
    ):
        raise HTTPException(410, "Platform invitation unavailable.")
    return row
