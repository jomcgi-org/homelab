"""Searchable play projections and transactional embedding audience copies."""

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import and_, case, null, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from grimoire.audience import Member, Viewer, audience_predicate, note_predicate
from grimoire.models import CharacterFact, Embedding, Note, SessionEvent
from grimoire.reveals import reveal_items

PLAY_KINDS = ("note", "event", "transcript", "fact")
EVENT_KINDS = ("narration", "handout", "reveal", "utterance")
_LOCK_ORDER = {"event": 0, "transcript": 0, "note": 1, "fact": 2}


def play_embedding_predicate(campaign_id: str, viewer: Viewer, member: Member):
    """One candidate clause, with the source audience contracts reused intact.

    Note kind is copied into audience. Deleted sources have their vectors removed
    transactionally; retrieval still checks deletion on the live row.
    """
    note_columns = SimpleNamespace(
        kind=Embedding.audience,
        author_member_id=Embedding.author_member_id,
        dm_readable=Embedding.dm_readable,
        deleted_at=null(),
    )
    return or_(
        and_(
            Embedding.embeddable_kind.in_(("entity", "chunk")),
            Embedding.campaign_id.is_(None),
        ),
        and_(
            Embedding.campaign_id == campaign_id,
            or_(
                and_(
                    Embedding.embeddable_kind.in_(("event", "transcript")),
                    audience_predicate(Embedding, viewer, member),
                ),
                and_(
                    Embedding.embeddable_kind == "fact",
                    # Like party notes, party facts require an associated PC (or DM).
                    viewer is not None,
                    Embedding.audience.in_(("table", "pcs")),
                    Embedding.author_member_id.is_(None),
                    audience_predicate(Embedding, viewer, member),
                ),
                and_(
                    Embedding.embeddable_kind == "note",
                    note_predicate(note_columns, viewer, member),
                ),
            ),
        ),
    )


def event_note_markdown(body: dict) -> str:
    """Human-readable snapshot of an already audience-filtered event body."""
    if "reveals" in body:
        return "\n\n".join(event_note_markdown(item) for item in reveal_items(body))
    if body.get("text"):
        return body["text"]
    title = body.get("name") or body.get("label") or "Session update"
    projection = body.get("projection") or body.get("entity") or body
    details = projection.get("revealed_details") or projection
    hidden = {
        "id",
        "entity_id",
        "name",
        "entity_type",
        "grant_scope",
        "source_type",
        "source_book",
        "site",
        "created_in_session",
        "created_at",
        "is_global",
        "recognition_only",
    }
    lines = [f"## {title}"]
    if body.get("grant_scope") == "name_only":
        lines.append("You recognize this name.")
    for key, value in details.items():
        if key in hidden or value is None:
            continue
        value_text = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
        )
        lines.append(f"**{key.replace('_', ' ').capitalize()}:** {value_text}")
    return "\n\n".join(lines)


def note_text(note: Note) -> str | None:
    if note.deleted_at is not None:
        return None
    return (
        "\n\n".join(part for part in (note.title, note.markdown) if part).strip()
        or None
    )


def event_embedding_kind(event: SessionEvent) -> str:
    return "transcript" if event.kind == "utterance" else "event"


def event_text(event: SessionEvent) -> str | None:
    if event.retracted_at is not None or event.kind not in EVENT_KINDS:
        return None
    # Until utterance ingest defines another contract, only literal ooc=True is OOC.
    if event.kind == "utterance" and event.body.get("ooc") is True:
        return None
    if event.kind != "reveal":
        return event_note_markdown(event.body).strip() if event.body else None
    texts = []
    for item in reveal_items(event.body):
        if item.get("grant_scope") == "name_only":
            continue  # ADR 011: recognition-only names never enter retrieval.
        projection = item.get("entity")
        if not isinstance(projection, dict):
            continue
        if item.get("grant_scope") == "partial":
            # Never fall back to the entity's full details for an empty partial grant.
            details = projection.get("revealed_details")
            if not isinstance(details, dict) or not details:
                continue
            projection = {"revealed_details": details}
        texts.append(
            event_note_markdown({"name": item.get("name"), "entity": projection})
        )
    return "\n\n".join(texts).strip() or None


def fact_text(fact: CharacterFact) -> str | None:
    return fact.statement if fact.status in ("active", "disputed") else None


def audience_columns(row: Note | SessionEvent | CharacterFact) -> dict:
    if isinstance(row, CharacterFact):
        return {
            "campaign_id": row.campaign_id,
            "audience": "table" if row.viewer_key == "party" else "pcs",
            "audience_pc_ids": [row.player_character_id]
            if row.player_character_id
            else [],
            "author_member_id": None,
            "dm_readable": None,
        }
    return {
        "campaign_id": row.campaign_id,
        "audience": row.kind if isinstance(row, Note) else row.audience,
        "audience_pc_ids": [] if isinstance(row, Note) else sorted(row.audience_pc_ids),
        "author_member_id": row.author_member_id,
        "dm_readable": row.dm_readable if isinstance(row, Note) else None,
    }


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sync_embeddings(
    session: Session, row: Note | SessionEvent | CharacterFact, kinds: tuple[str, ...]
) -> None:
    source_text = _text(row)
    expected_kind = _kind(row)
    embeddings = session.exec(
        select(Embedding).where(
            Embedding.embeddable_kind.in_(kinds),
            Embedding.embeddable_id == row.id,
        )
    ).all()
    for embedding in embeddings:
        if source_text is None or embedding.embeddable_kind != expected_kind:
            session.delete(embedding)
        elif (
            isinstance(row, SessionEvent)
            and row.kind == "reveal"
            and embedding.content_hash != content_hash(source_text)
        ):
            # Revoked grouped reveal content must not leave a searchable stale vector.
            session.delete(embedding)
        else:
            for key, value in audience_columns(row).items():
                setattr(embedding, key, value)


def sync_note_embeddings(session: Session, note: Note) -> None:
    _sync_embeddings(session, note, ("note",))


def sync_event_embeddings(session: Session, event: SessionEvent) -> None:
    from grimoire.character_facts import retract_facts_for_event

    _sync_embeddings(session, event, ("event", "transcript"))
    retract_facts_for_event(session, event)


def sync_fact_embeddings(session: Session, fact: CharacterFact) -> None:
    _sync_embeddings(session, fact, ("fact",))


@dataclass(frozen=True)
class PlayEmbeddingInput:
    kind: str
    source_id: str
    text: str


def _source(session: Session, kind: str, source_id: str):
    model = {"note": Note, "fact": CharacterFact}.get(kind, SessionEvent)
    return session.exec(
        select(model)
        .where(model.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def _text(row):
    if isinstance(row, CharacterFact):
        return fact_text(row)
    return note_text(row) if isinstance(row, Note) else event_text(row)


def _kind(row):
    if isinstance(row, CharacterFact):
        return "fact"
    return "note" if isinstance(row, Note) else event_embedding_kind(row)


def collect_play_inputs(
    session: Session, model: str, limit: int
) -> list[PlayEmbeddingInput]:
    """Remove obsolete vectors and collect at most limit changed source projections."""
    for embedding in session.exec(
        select(Embedding)
        .where(
            Embedding.embeddable_kind.in_(PLAY_KINDS),
        )
        .order_by(
            case(
                (Embedding.embeddable_kind.in_(("event", "transcript")), 0),
                (Embedding.embeddable_kind == "note", 1),
                else_=2,
            ),
            Embedding.embeddable_id,
            Embedding.id,
        )
        .execution_options(yield_per=500)
    ):
        row = _source(session, embedding.embeddable_kind, embedding.embeddable_id)
        if (
            row is None
            or _text(row) is None
            or (
                isinstance(row, SessionEvent)
                and event_embedding_kind(row) != embedding.embeddable_kind
            )
        ):
            session.delete(embedding)
    inputs = []
    for source_model, predicate in (
        (
            SessionEvent,
            SessionEvent.retracted_at.is_(None) & SessionEvent.kind.in_(EVENT_KINDS),
        ),
        (Note, Note.deleted_at.is_(None)),
        (CharacterFact, CharacterFact.status.in_(("active", "disputed"))),
    ):
        for row in session.exec(
            select(source_model)
            .where(predicate)
            .order_by(source_model.id)
            .execution_options(yield_per=500)
        ):
            source_text = _text(row)
            if source_text is None:
                continue
            kind = _kind(row)
            embedding = session.exec(
                select(Embedding).where(
                    Embedding.embeddable_kind == kind,
                    Embedding.embeddable_id == row.id,
                    Embedding.model == model,
                )
            ).one_or_none()
            if (
                embedding is None
                or embedding.content_hash != content_hash(source_text)
                or any(
                    getattr(embedding, key) != value
                    for key, value in audience_columns(row).items()
                )
            ):
                inputs.append(PlayEmbeddingInput(kind, row.id, source_text))
                if len(inputs) == limit:
                    return inputs
    return inputs


def persist_play_vectors(
    session: Session,
    model: str,
    inputs: list[PlayEmbeddingInput],
    vectors: list[list[float]],
) -> int:
    """Lock and re-check sources after network I/O, then upsert in this transaction."""
    if len(inputs) != len(vectors):
        raise ValueError("embedding batch returned the wrong number of vectors")
    values = []
    # Retraction and the writer lock events before dependent facts. Preserve
    # that order even for caller-supplied batches; keep vectors paired on sort.
    ordered = sorted(
        zip(inputs, vectors, strict=True),
        key=lambda pair: (_LOCK_ORDER[pair[0].kind], pair[0].source_id),
    )
    for item, vector in ordered:
        row = _source(session, item.kind, item.source_id)
        if (
            row is None
            or _text(row) != item.text
            or (
                isinstance(row, SessionEvent) and event_embedding_kind(row) != item.kind
            )
        ):
            continue
        values.append(
            {
                "id": str(uuid4()),
                "embeddable_kind": item.kind,
                "embeddable_id": item.source_id,
                "model": model,
                "dim": len(vector),
                "vector": vector,
                "content_hash": content_hash(item.text),
                **audience_columns(row),
            }
        )
    if values:
        insert = (
            sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
        )
        statement = insert(Embedding).values(values)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["embeddable_kind", "embeddable_id", "model"],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in (
                        "dim",
                        "vector",
                        "content_hash",
                        "campaign_id",
                        "audience",
                        "audience_pc_ids",
                        "author_member_id",
                        "dm_readable",
                    )
                },
            )
        )
    return len(values)
