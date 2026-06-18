"""Read-only SQLModel mappings for the public_api views.

These map to Postgres VIEWS (public_api.knowledge_notes / knowledge_note_links)
created by migration 20260617020000. They exist so the public knowledge
handlers can read the public surface as the public_reader role, which has no
access to the knowledge schema. In SQLite tests the real_session fixture strips
the schema and create_all materializes them as plain tables that tests seed
directly; the view derivation + permissions are covered by a real-Postgres test.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column
from sqlmodel import Field, SQLModel

# Reuse the exact array column type the Note model uses (Postgres TEXT[] with a
# SQLite JSON fallback) so tags/aliases round-trip identically through both the
# real-Postgres view and the create_all-materialized SQLite test table.
from knowledge.models import _STRING_ARRAY


class PublicNote(SQLModel, table=True):
    """Maps to the public_api.knowledge_notes view (public, non-deleted notes).

    The view has no primary key; declaring ``note_id`` as one lets the ORM map
    rows. It is never enforced (the model is never CREATEd in production: the
    migration owns the DDL).
    """

    __tablename__ = "knowledge_notes"
    __table_args__ = {"schema": "public_api", "extend_existing": True}

    note_id: str = Field(primary_key=True)
    title: str
    type: str | None = None
    content: str | None = None
    indexed_at: datetime
    layout_x: float | None = None
    layout_y: float | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(_STRING_ARRAY))
    aliases: list[str] = Field(default_factory=list, sa_column=Column(_STRING_ARRAY))
    path: str


class PublicNoteLink(SQLModel, table=True):
    """Maps to the public_api.knowledge_note_links view (source-public links)."""

    __tablename__ = "knowledge_note_links"
    __table_args__ = {"schema": "public_api", "extend_existing": True}

    id: int = Field(primary_key=True)
    source: str
    target: str
    kind: str
    edge_type: str | None = None


class PublicChunk(SQLModel, table=True):
    """Maps to the public_api.knowledge_chunks view (chunks of public notes).

    Created by migration 20260617040000. It joins knowledge.chunks to
    knowledge.notes and exposes only chunks of public, non-deleted notes, so the
    public-chat retrieval path (running as public_reader) can run a pgvector cosine
    search over public embeddings without any access to the knowledge schema.

    The view has no primary key; (note_id, chunk_index) is unique per row, so the
    pair is declared as a composite key purely to let the ORM map rows. It is never
    enforced (the model is never CREATEd in production: the migration owns the DDL).
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = {"schema": "public_api", "extend_existing": True}

    note_id: str = Field(primary_key=True)
    chunk_index: int = Field(primary_key=True)
    title: str
    section_header: str = ""
    chunk_text: str
    embedding: list[float] = Field(sa_column=Column(Vector(1024)))
