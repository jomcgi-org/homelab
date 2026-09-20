"""Caller-derived authorization for public knowledge search entrypoints."""

from __future__ import annotations

import json
from dataclasses import dataclass

from auth.api import Authority, Principal
from sqlalchemy import insert
from sqlmodel import Session

from knowledge.models import PersonalRetrievalAudit

_DEFAULT_SCOPE_PREFIXES = ("org:", "repo:", "environment:")
_HOMELAB_RETRIEVAL_SCOPES = (
    "org:jomcgi-org",
    "repo:jomcgi-org/homelab",
    "environment:homelab",
)
# These are resource grants, not identity aliases. Both group names and their
# memberships are declared by the authentik MCP blueprint. The generic
# ``operators`` group is deliberately absent because operator status alone does
# not name a repository or environment.
_GROUP_SCOPE_GRANTS = {
    "homelab-admin": _HOMELAB_RETRIEVAL_SCOPES,
    "kg-agents": _HOMELAB_RETRIEVAL_SCOPES,
}
_PERSONAL_SCOPE_GROUPS = frozenset(_GROUP_SCOPE_GRANTS)
_SUBJECT_CAP = 512
_ACTOR_CHAIN_CAP = 4_096
_ACTOR_ITEMS_CAP = 16
_PERSONAL_SCOPE_CAP = 1_024


class RetrievalAuthorizationError(ValueError):
    """A verified principal cannot authorize the requested retrieval."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RetrievalAuditError(RuntimeError):
    """A personal retrieval could not be durably audited."""


@dataclass(frozen=True, slots=True)
class RetrievalAuthorization:
    """Exact database scopes authorized for one search."""

    scopes: tuple[str, ...]
    include_unscoped: bool
    personal_scope: str | None


def authorize_retrieval(
    principal: Principal, *, include_personal: bool
) -> RetrievalAuthorization:
    """Derive exact allowed scopes from verified principal grants.

    The ``scope`` and ``groups`` claims are already signature-verified by the
    auth layer. Only exact knowledge grants and explicit resource-group mappings
    are consumed here. Generic OAuth scopes, request strings, and the server's
    default repository setting grant nothing.
    """
    if principal.authority is Authority.ANONYMOUS:
        raise RetrievalAuthorizationError(
            "anonymous", "knowledge search requires authentication"
        )

    exact_scope_grants = [
        scope
        for scope in principal.scope
        if scope.startswith(_DEFAULT_SCOPE_PREFIXES) and len(scope.split(":", 1)[1]) > 0
    ]
    group_scope_grants = [
        scope
        for group in principal.groups
        for scope in _GROUP_SCOPE_GRANTS.get(group, ())
    ]
    default_scopes = tuple(dict.fromkeys((*exact_scope_grants, *group_scope_grants)))
    personal_scope = f"personal:{principal.subject}"

    if include_personal:
        personal_granted = personal_scope in principal.scope or any(
            group in _PERSONAL_SCOPE_GROUPS for group in principal.groups
        )
        if not personal_granted:
            raise RetrievalAuthorizationError(
                "personal_scope_not_granted",
                "personal knowledge scope is not granted to this principal",
            )
        scopes = (*default_scopes, personal_scope)
        return RetrievalAuthorization(
            scopes=tuple(dict.fromkeys(scopes)),
            include_unscoped=True,
            personal_scope=personal_scope,
        )

    if not default_scopes:
        raise RetrievalAuthorizationError(
            "unmapped_principal",
            "principal has no knowledge retrieval scopes",
        )
    return RetrievalAuthorization(
        scopes=default_scopes,
        include_unscoped=False,
        personal_scope=None,
    )


def audit_personal_retrieval(
    session: Session,
    principal: Principal,
    authorization: RetrievalAuthorization,
    *,
    entrypoint: str,
) -> None:
    """Commit exactly one bounded audit row before personal data is queried."""
    if authorization.personal_scope is None:
        return

    actor_chain = json.dumps(list(principal.actor), separators=(",", ":"))
    if (
        len(principal.subject) > _SUBJECT_CAP
        or len(principal.actor) > _ACTOR_ITEMS_CAP
        or len(actor_chain) > _ACTOR_CHAIN_CAP
        or len(authorization.personal_scope) > _PERSONAL_SCOPE_CAP
    ):
        raise RetrievalAuditError("principal attribution exceeds audit bounds")

    try:
        # Use a Core insert without RETURNING. PostgreSQL requires SELECT on
        # every returned column, while the agents tier deliberately has only
        # INSERT on this attribution table and USAGE on its identity sequence.
        session.execute(
            insert(PersonalRetrievalAudit.__table__).values(
                principal_subject=principal.subject,
                principal_actor=actor_chain,
                principal_authority=principal.authority.value,
                personal_scope=authorization.personal_scope,
                entrypoint=entrypoint,
            )
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        raise RetrievalAuditError("personal retrieval audit unavailable") from exc
