"""Server-projected deployment observations for the environment scope.

The cd health poller supplies Kubernetes, Kargo, and GitHub evidence. This
module persists that evidence directly as one immutable raw and one fact, with
no LLM extraction step. Event identity includes the poll time, so replaying a
poll is idempotent while a later poll with unchanged versions remains a new
observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import or_
from sqlmodel import Session, select
import yaml

from knowledge.indexing import index_note_from_raw
from knowledge.models import AtomRawProvenance, Note, RawInput
from knowledge.raw_write import persist_raw_with_status
from knowledge.store import KnowledgeStore, provenance_for_notes
from shared.embedding import EmbeddingClient

DEPLOYMENT_OBSERVATION_SCOPE = "environment:homelab"
DEPLOYMENT_OBSERVATION_SOURCE = "deployment-observation"
DEPLOYMENT_OBSERVATION_VERSION = "deployment-observation/v1"
VALIDITY_MULTIPLIER = 2.5

_REQUIRED_FIELDS = frozenset(
    {
        "event_type",
        "scope",
        "app",
        "status",
        "observed_at",
        "valid_from",
        "valid_until",
    }
)
_OPTIONAL_FIELDS = frozenset(
    {
        "requested_revision",
        "deployed_revision",
        "newest_freight_version",
        "writeback_commit",
        "error_stage",
    }
)
DEPLOYMENT_OBSERVATION_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


@dataclass(frozen=True)
class ObservationWriteResult:
    raw_created: bool
    fact_created: bool
    provenance_created: bool
    raw_id: str
    note_id: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_observation(observation: dict[str, Any]) -> None:
    fields = frozenset(observation)
    missing = _REQUIRED_FIELDS - fields
    unknown = fields - DEPLOYMENT_OBSERVATION_FIELDS
    if missing:
        raise ValueError(f"deployment observation missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(
            f"deployment observation has forbidden fields: {sorted(unknown)}"
        )
    if observation["event_type"] != DEPLOYMENT_OBSERVATION_SOURCE:
        raise ValueError("deployment observation event_type is invalid")
    if observation["scope"] != DEPLOYMENT_OBSERVATION_SCOPE:
        raise ValueError("deployment observation scope is invalid")
    if observation["status"] not in {
        "complete",
        "missing_provenance",
        "upstream_error",
    }:
        raise ValueError("deployment observation status is invalid")


def build_deployment_observation(
    *,
    app: str,
    status: str,
    poll_time: datetime,
    probe_interval_s: float,
    requested_revision: str | None = None,
    deployed_revision: str | None = None,
    newest_freight_version: str | None = None,
    writeback_commit: str | None = None,
    error_stage: str | None = None,
) -> dict[str, Any]:
    """Build the strict deployment-only payload persisted by the poller."""
    observed_at = _utc(poll_time)
    valid_until = observed_at.timestamp() + probe_interval_s * VALIDITY_MULTIPLIER
    payload: dict[str, Any] = {
        "event_type": DEPLOYMENT_OBSERVATION_SOURCE,
        "scope": DEPLOYMENT_OBSERVATION_SCOPE,
        "app": app,
        "status": status,
        "observed_at": observed_at.isoformat(),
        "valid_from": observed_at.isoformat(),
        "valid_until": datetime.fromtimestamp(valid_until, timezone.utc).isoformat(),
    }
    optional = {
        "requested_revision": requested_revision,
        "deployed_revision": deployed_revision,
        "newest_freight_version": newest_freight_version,
        "writeback_commit": writeback_commit,
        "error_stage": error_stage,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    _validate_observation(payload)
    return payload


def _event_id(observation: dict[str, Any]) -> str:
    identity = json.dumps(
        {
            "scope": observation["scope"],
            "app": observation["app"],
            "observed_at": observation["observed_at"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _fact_markdown(observation: dict[str, Any], note_id: str) -> str:
    app = observation["app"]
    observed_at = observation["observed_at"]
    metadata: dict[str, Any] = {
        "id": note_id,
        "title": f"Deployment observation for {app} at {observed_at}",
        "type": "fact",
        "visibility": "private",
        "source": DEPLOYMENT_OBSERVATION_SOURCE,
        "scope": DEPLOYMENT_OBSERVATION_SCOPE,
        "verification_state": "verified",
        "valid_from": observation["valid_from"],
        "valid_until": observation["valid_until"],
        "observed_at": observed_at,
        "tags": ["deployment-observation", app],
    }
    metadata.update(
        {
            key: value
            for key, value in observation.items()
            if key
            not in {
                "event_type",
                "scope",
                "observed_at",
                "valid_from",
                "valid_until",
            }
        }
    )
    frontmatter = yaml.safe_dump(metadata, sort_keys=False).rstrip()
    body = json.dumps(observation, sort_keys=True, indent=2)
    return f"---\n{frontmatter}\n---\n\n```json\n{body}\n```\n"


async def persist_deployment_observation(
    session: Session,
    observation: dict[str, Any],
    *,
    vectors: list[list[float]] | None = None,
) -> ObservationWriteResult:
    """Persist an observation and repair any partial replay idempotently."""
    _validate_observation(observation)
    event_id = _event_id(observation)
    note_id = f"deployment-observation-{event_id}"
    existing_note = session.exec(
        select(Note).where(Note.note_id == note_id)
    ).one_or_none()
    if existing_note is not None:
        existing_provenance = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.atom_fk == existing_note.id,
                AtomRawProvenance.raw_fk.is_not(None),
            )
        ).first()
        if existing_provenance is not None:
            existing_raw = session.get(RawInput, existing_provenance.raw_fk)
            if existing_raw is not None:
                return ObservationWriteResult(
                    raw_created=False,
                    fact_created=False,
                    provenance_created=False,
                    raw_id=existing_raw.raw_id,
                    note_id=note_id,
                )

    raw_body = json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n"
    raw, raw_created = persist_raw_with_status(
        session,
        content=raw_body,
        source=DEPLOYMENT_OBSERVATION_SOURCE,
        scope=DEPLOYMENT_OBSERVATION_SCOPE,
        status=observation["status"],
        validity_hint=observation["valid_until"],
        extra={"event_id": event_id, "projection": "server"},
        commit=False,
    )

    note = session.exec(select(Note).where(Note.note_id == note_id)).one_or_none()
    fact_created = note is None
    if note is None:
        await index_note_from_raw(
            KnowledgeStore(session),
            EmbeddingClient(),
            note_id=note_id,
            rel_path=f"_processed/{note_id}.md",
            raw=_fact_markdown(observation, note_id),
            vectors=vectors,
            commit=False,
        )
        note = session.exec(select(Note).where(Note.note_id == note_id)).one()

    provenance = session.exec(
        select(AtomRawProvenance).where(
            AtomRawProvenance.atom_fk == note.id,
            AtomRawProvenance.raw_fk == raw.id,
        )
    ).one_or_none()
    provenance_created = provenance is None
    if provenance is None:
        session.add(
            AtomRawProvenance(
                atom_fk=note.id,
                raw_fk=raw.id,
                gardener_version=DEPLOYMENT_OBSERVATION_VERSION,
            )
        )
    session.commit()
    return ObservationWriteResult(
        raw_created=raw_created,
        fact_created=fact_created,
        provenance_created=provenance_created,
        raw_id=raw.raw_id,
        note_id=note_id,
    )


def list_deployment_observations(
    session: Session,
    *,
    active_as_of: datetime | None = None,
    app: str | None = None,
) -> list[dict[str, Any]]:
    """Return observation history, optionally restricted to one active instant.

    With no ``active_as_of`` all historical rows remain inspectable. Active
    lookup treats validity as a half-open interval: ``[valid_from, valid_until)``.
    """
    statement = select(Note).where(
        Note.scope == DEPLOYMENT_OBSERVATION_SCOPE,
        Note.source == DEPLOYMENT_OBSERVATION_SOURCE,
        Note.deleted_at.is_(None),
    )
    if active_as_of is not None:
        instant = _utc(active_as_of)
        statement = statement.where(
            or_(Note.valid_from.is_(None), Note.valid_from <= instant),
            or_(Note.valid_until.is_(None), Note.valid_until > instant),
        )
    statement = statement.order_by(Note.observed_at.asc(), Note.note_id.asc())
    notes = list(session.exec(statement).all())
    if app is not None:
        notes = [note for note in notes if note.extra.get("app") == app]
    provenance = provenance_for_notes(session, [note.note_id for note in notes])
    return [
        {
            "note_id": note.note_id,
            "scope": note.scope,
            "observed_at": note.observed_at,
            "valid_from": note.valid_from,
            "valid_until": note.valid_until,
            "status": note.status,
            **dict(note.extra),
            "provenance": provenance.get(note.note_id, []),
        }
        for note in notes
    ]
