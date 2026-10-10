"""DM handouts: body validation, upload sniffing and image lookup rules.

A ``handout`` session event carries ``{title, markdown, entity_id?, image?}``.
``image`` is either an open-licensed corpus chunk image
(``{"source": "chunk", "chunk_id": ...}``) or an image the DM uploaded under
``campaigns/<campaign_id>/handouts/`` in the grimoire bucket
(``{"source": "upload", "key": ...}``). Both are served only through the
members-only event image route, never by URL, so the audience predicate decides
who can fetch the bytes.

The stored body is the NORMALISED validated dict (stable key set, ``None``
dropped, canonical UUID spelling) so ``append_event``'s request_id retry
comparison stays idempotent for an identical resubmission.
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlmodel import Session, select

from grimoire import library
from grimoire.models import Entity, KnowledgeChunk
from grimoire.visibility import entity_belongs_to_campaign

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_EXTENSIONS = ("png", "jpg", "webp", "gif")
IMAGE_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


class HandoutInvalidError(ValueError):
    """The handout body breaks the contract; callers map it to a 422."""


class ChunkImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["chunk"]
    chunk_id: UUID


class UploadImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["upload"]
    key: str = Field(min_length=1, max_length=300)


class HandoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    markdown: str = Field(max_length=20000)
    entity_id: UUID | None = None
    image: Annotated[ChunkImage | UploadImage, Field(discriminator="source")] | None = (
        None
    )


def bucket() -> str:
    from grimoire.jobs import DEFAULT_BUCKET

    return os.environ.get("GRIMOIRE_S3_BUCKET", DEFAULT_BUCKET)


def upload_key_pattern(campaign_id: str) -> re.Pattern[str]:
    return re.compile(
        rf"campaigns/{re.escape(campaign_id)}/handouts/[0-9a-f]{{32}}"
        rf"\.({'|'.join(UPLOAD_EXTENSIONS)})"
    )


def new_upload_key(campaign_id: str, ext: str) -> str:
    return f"campaigns/{campaign_id}/handouts/{uuid4().hex}.{ext}"


def valid_upload_key(campaign_id: str, key: object) -> bool:
    return isinstance(key, str) and (
        upload_key_pattern(campaign_id).fullmatch(key) is not None
    )


def open_licensed_image_chunk(session: Session, chunk_id: str) -> KnowledgeChunk | None:
    """The chunk when it carries an image from an open-licensed book, else None.

    Fails closed: a copyrighted book, an unclassified (missing or NULL) book and
    a chunk without an ``image_ref`` all refuse.
    """
    chunk = session.get(KnowledgeChunk, str(chunk_id))
    if chunk is None or not chunk.image_ref:
        return None
    if library.is_book_copyrighted(session, chunk.book_id):
        return None
    return chunk


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """``(mime, ext)`` from magic bytes for PNG, JPEG, GIF and WebP, else None.

    The client's declared content type is never consulted.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _entity_in_campaign(session: Session, campaign_id: str, entity_id: UUID) -> bool:
    canonical = str(entity_id)
    # SQLite fixtures keep the persisted spelling, which may be uppercase.
    entity = session.exec(
        select(Entity).where(Entity.id.in_({canonical, canonical.upper()}))
    ).first()
    return entity is not None and entity_belongs_to_campaign(
        session, campaign_id, entity
    )


def validate_handout_body(
    session: Session, campaign_id: str, raw: dict[str, Any]
) -> dict[str, Any]:
    """Validate a raw handout body and return its normalised storable dict."""
    try:
        parsed = HandoutBody.model_validate(raw)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise HandoutInvalidError(f"invalid handout: {detail}") from exc
    if parsed.entity_id is not None and not _entity_in_campaign(
        session, campaign_id, parsed.entity_id
    ):
        raise HandoutInvalidError("handout entity not found in this campaign")
    image = parsed.image
    if isinstance(image, ChunkImage):
        if open_licensed_image_chunk(session, str(image.chunk_id)) is None:
            raise HandoutInvalidError("handout chunk image is not available")
    elif isinstance(image, UploadImage):
        if not valid_upload_key(campaign_id, image.key):
            raise HandoutInvalidError("handout upload key is not valid")
    return parsed.model_dump(mode="json", exclude_none=True)


def handout_entity_ids(events) -> set[str]:
    """Canonical entity ids referenced by handout rows, for visibility lookup."""
    ids = set()
    for row in events:
        value = (row.body or {}).get("entity_id") if row.kind == "handout" else None
        if not isinstance(value, str):
            continue
        try:
            ids.add(str(UUID(value)))
        except ValueError:
            continue
    return ids


def project_handout_body(
    body: dict[str, Any], visible_entity_ids: set[str] | frozenset[str]
) -> dict[str, Any]:
    """Drop ``entity_id`` unless the viewer can already see that entity."""
    value = body.get("entity_id")
    if value is None:
        return body
    try:
        keep = str(UUID(value)) in visible_entity_ids
    except (ValueError, TypeError, AttributeError):
        keep = False
    if keep:
        return body
    return {key: item for key, item in body.items() if key != "entity_id"}
