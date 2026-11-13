"""Conservative, local chunk taxonomy. No inference or book-wide labels."""

from __future__ import annotations

import re
from typing import Literal

from sqlmodel import Session, select

from grimoire.models import Embedding, KnowledgeChunk

Topic = Literal["dm_guidance"]


def validate_topic(topic: str | None) -> str | None:
    if topic is not None and topic != "dm_guidance":
        raise ValueError("Unknown chunk topic")
    return topic


# Require actionable heading evidence AND advice in this chunk. An ancestor
# named after a book or a generic word such as 'encounters' is insufficient.
_HEADING = re.compile(
    r"\b(?:getting players invested|running (?:the game|a campaign|adventures|encounters)|"
    r"(?:creating|building|designing) (?:a campaign|adventures|encounters|conflicts)|"
    r"(?:campaign|adventure|encounter) (?:design|pacing)|"
    r"(?:managing|adjusting) (?:pacing|players)|bringing .{1,60} to life)\b",
    re.I,
)
_ADVICE = re.compile(
    r"\b(?:you (?:can|should|might)|consider|avoid|try to|ask (?:your|the) players)\b",
    re.I,
)
_SUBJECT = re.compile(
    r"\b(?:players|characters|campaign|adventure|encounters?|pacing|conflicts?)\b", re.I
)
_DM = re.compile(
    r"\b(?:as (?:a|the) (?:dm|dungeon master)|"
    r"when (?:running|designing|preparing) (?:your|a|an) (?:campaign|adventure|encounter))\b",
    re.I,
)


def classify_topic(
    content: str, section_path: str | None, section_hierarchy: str | None
) -> Topic | None:
    """False negatives are acceptable; mundane book prose stays unclassified.

    Book identity never supplies evidence. Hierarchy is useful only alongside
    advice in the current content, including setting-guide prose addressed to DMs.
    """
    headings = " > ".join(filter(None, (section_path, section_hierarchy)))
    if (
        _ADVICE.search(content)
        and _SUBJECT.search(content)
        and (_HEADING.search(headings) or _DM.search(content))
    ):
        return "dm_guidance"
    return None


def topic_embedding_predicate(topic: str):
    """Correlate live chunks before vector ranking, using UUID equality."""
    validate_topic(topic)
    return (Embedding.embeddable_kind == "chunk") & (
        select(KnowledgeChunk.id)
        .where(
            KnowledgeChunk.id == Embedding.embeddable_id,
            KnowledgeChunk.topic == topic,
        )
        .exists()
    )


def classify_stored_chunks(
    session: Session, *, book_id: str, after_ref: str | None = None, limit: int = 100
) -> dict:
    """Explicit staged backfill page; caller owns commit/rollback. No scheduler.

    Keyset by immutable per-book chunk_ref. Replaying a page is idempotent.
    Only topic metadata is written; vectors and extraction markers are untouched.
    """
    if not book_id or not 1 <= limit <= 500:
        raise ValueError("book_id and a limit from 1 to 500 are required")
    query = select(KnowledgeChunk).where(KnowledgeChunk.book_id == book_id)
    if after_ref is not None:
        query = query.where(KnowledgeChunk.chunk_ref > after_ref)
    rows = session.exec(
        query.order_by(KnowledgeChunk.chunk_ref)
        .limit(limit)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    changed = 0
    for row in rows:
        topic = classify_topic(row.content, row.section_path, row.section_hierarchy)
        if row.topic != topic:
            row.topic = topic
            changed += 1
    return {
        "scanned": len(rows),
        "changed": changed,
        "next_ref": rows[-1].chunk_ref if len(rows) == limit else None,
    }
