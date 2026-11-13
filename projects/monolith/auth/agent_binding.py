"""Server-only admitted write identity contract; no production resolver exists."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from auth.principal import Principal


@dataclass(frozen=True, slots=True)
class AgentWriteBinding:
    """Resolver attestation of a live admission or distinct drainer identity.

    Resolvers must consult trusted live admission/revocation state, bind the
    exact verified principal, and never derive authority from request content.
    A guest requires its admitted live session. This fixture/composition
    contract supplies no credential delivery or deployed identity path.
    """

    principal: Principal
    principal_class: str
    producer_id: str
    expires_at: datetime
    session_id: str | None = None
    revoked: bool = False

    def valid_for(self, principal: Principal) -> bool:
        return (
            self.principal == principal
            and self.principal_class in {"guest", "drainer"}
            and isinstance(self.producer_id, str)
            and bool(self.producer_id.strip())
            and isinstance(self.expires_at, datetime)
            and self.expires_at.tzinfo is not None
            and self.expires_at > datetime.now(timezone.utc)
            and self.revoked is False
            and (
                self.session_id is None
                or (isinstance(self.session_id, str) and bool(self.session_id.strip()))
            )
            and (
                self.principal_class != "guest"
                or (isinstance(self.session_id, str) and bool(self.session_id.strip()))
            )
        )


current_write_binding: ContextVar[AgentWriteBinding | None] = ContextVar(
    "agent_write_binding", default=None
)


def producer_extra(principal: Principal) -> dict[str, str]:
    """Return only request-local, validated server attribution."""
    binding = current_write_binding.get()
    if binding is None or not binding.valid_for(principal):
        return {}
    extra = {
        "producer_id": binding.producer_id,
        "producer_class": binding.principal_class,
    }
    if binding.session_id is not None:
        extra["producer_session_id"] = binding.session_id
    return extra
