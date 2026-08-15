"""Request identity facts, without resource authorization policy."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class PrincipalKind(enum.StrEnum):
    """The class of identity represented by a principal."""

    HUMAN = "human"
    WORKLOAD = "workload"


class Authority(enum.StrEnum):
    """How the principal's authority was established."""

    STANDING = "standing"
    DELEGATED = "delegated"
    ANONYMOUS = "anonymous"


@dataclass(frozen=True, slots=True)
class Principal:
    """Verified identity and carried grants for one request."""

    subject: str
    actor: tuple[str, ...]
    scope: tuple[str, ...]
    groups: tuple[str, ...]
    email: str | None
    kind: PrincipalKind
    authority: Authority

    def has_group(self, name: str) -> bool:
        return name in self.groups

    def has_scope(self, name: str) -> bool:
        return name in self.scope


def anonymous_principal() -> Principal:
    """Return the least-privilege principal used when no bearer token exists."""

    return Principal(
        subject="anonymous",
        actor=(),
        scope=(),
        groups=(),
        email=None,
        # Anonymous is an authority state, not a third identity kind.
        kind=PrincipalKind.HUMAN,
        authority=Authority.ANONYMOUS,
    )
