"""Note-event payload schemas (entity_type ``note``).

Mirrors what the Wavefront-5 backfill will publish onto
``events.knowledge.note``: the note record plus its chunked, embedded content.
Embeddings are 1024-dim ``voyage-4-nano`` vectors.

Note text and embeddings are sensitive KG content (ADR 017 §Security) — these
payloads are the same sensitivity tier as the knowledge graph itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# voyage-4-nano embedding dimensionality.
EMBEDDING_DIM = 1024


class NoteChunk(BaseModel):
    """One embedded chunk of a note's body."""

    model_config = ConfigDict(extra="allow")

    chunk_index: int
    section_header: str | None = None
    chunk_text: str
    # 1024-dim voyage-4-nano vector. Length is documented, not enforced, so the
    # schema tolerates a future embedding-model swap without a version bump.
    embedding: list[float]


class NoteCreatedPayload(BaseModel):
    """Payload for a ``note`` ``created`` event.

    Carries note metadata plus the full set of embedded chunks. ``content_hash``
    lets consumers dedup / detect no-op republishes of unchanged note bodies.
    """

    model_config = ConfigDict(extra="allow")

    note_id: str
    path: str
    title: str
    content_hash: str
    type: str
    status: str
    visibility: str
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    chunks: list[NoteChunk] = Field(default_factory=list)


class NoteUpdatedPayload(NoteCreatedPayload):
    """Payload for a ``note`` ``updated`` event — same shape as created."""


class NoteTombstonedPayload(BaseModel):
    """Payload for a ``note`` ``tombstoned`` event.

    References the note and a redacted reason only; does not carry the note
    body or embeddings being forgotten (ADR 017 §Security, RTBF).
    """

    model_config = ConfigDict(extra="allow")

    note_id: str
    reason: str | None = None


def register(schemas: dict[str, dict]) -> None:
    """Register note payload models into the shared SCHEMAS dict."""
    schemas.setdefault("note", {}).update(
        {
            "created": NoteCreatedPayload,
            "updated": NoteUpdatedPayload,
            "tombstoned": NoteTombstonedPayload,
        }
    )
