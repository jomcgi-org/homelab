"""Trusted database routing for Grimoire campaign-local state.

The campaign registry stays in ``grimoire.campaign``. Mutable play state lives
in a schema whose name is deterministically derived from the registry UUID.
Callers never accept a schema name from HTTP input and never mutate
``search_path``. SQLAlchemy's per-engine ``schema_translate_map`` instead
qualifies every statement, which keeps pooled connections safe when requests
for different campaigns reuse the same physical connection.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlmodel import Session

from grimoire.models import Campaign

CAMPAIGN_SCHEMA_PREFIX = "grimoire_campaign_"
_CAMPAIGN_SCHEMA_RE = re.compile(r"^grimoire_campaign_[0-9a-f]{32}$")


def campaign_schema_name(campaign_id: str) -> str:
    """Return the only valid schema name for a campaign UUID."""
    try:
        canonical_id = uuid.UUID(campaign_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("campaign id must be a UUID") from exc
    return f"{CAMPAIGN_SCHEMA_PREFIX}{canonical_id.hex}"


def validate_campaign_schema(campaign: Campaign) -> str:
    """Fail closed if persisted routing metadata is malformed or inconsistent."""
    expected = campaign_schema_name(campaign.id)
    schema_name = campaign.schema_name
    if schema_name != expected or not _CAMPAIGN_SCHEMA_RE.fullmatch(schema_name):
        raise RuntimeError("campaign registry contains invalid schema routing")
    return schema_name


def provision_campaign_schema(session: Session, campaign: Campaign) -> None:
    """Provision a campaign schema in the registry transaction.

    PostgreSQL DDL is transactional, so the registry row and schema become
    visible together. SQLite is used only by unit tests and has no schemas;
    those tests exercise the same route logic in one namespace while the
    real-Postgres regression test covers physical isolation.
    """
    if session.get_bind().dialect.name == "sqlite":
        return
    session.execute(
        text("SELECT grimoire.provision_campaign_schema(:campaign_id)"),
        params={"campaign_id": campaign.id},
    )


@contextmanager
def campaign_session(
    registry_session: Session, campaign: Campaign
) -> Iterator[Session]:
    """Yield a session routed to one trusted campaign schema.

    The ``campaign`` symbolic schema holds local mutable tables. The existing
    ``grimoire`` model schema is translated to campaign read views, which expose
    the shared corpus plus this campaign's homebrew overlay. The registry lookup
    itself always happens through ``registry_session`` before this function.
    """
    schema_name = validate_campaign_schema(campaign)
    bind = registry_session.get_bind()
    if bind.dialect.name == "sqlite":
        yield registry_session
        return

    # A test or outer transaction may bind the registry Session to a Connection.
    # Derive from its Engine so execution_options cannot mutate that live
    # Connection in place. Production Sessions are already Engine-bound.
    engine = bind.engine if hasattr(bind, "engine") else bind
    routed_bind = engine.execution_options(
        schema_translate_map={
            "campaign": schema_name,
            "grimoire": schema_name,
        }
    )
    with Session(routed_bind, expire_on_commit=False) as routed_session:
        yield routed_session
