"""Private, staged retrieval for the future #6630 campaign context builder.

No route or assistant calls this helper yet. The caller must authenticate the
principal and supply their authorized campaign member id. This helper rechecks
that membership against live storage, including after query embedding.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlmodel import Session

from grimoire.models import CampaignMember, KnowledgeChunk
from grimoire.search import _Embedder, search_knowledge_vector


@dataclass(frozen=True)
class DMGuidanceContext:
    campaign_id: str
    authorized_member_id: str


def _dm_member(session: Session, context: DMGuidanceContext) -> CampaignMember:
    member = session.get(
        CampaignMember, context.authorized_member_id, populate_existing=True
    )
    if (
        member is None
        or member.campaign_id != context.campaign_id
        or member.role != "dm"
    ):
        raise PermissionError("Guidance context requires an authorized campaign DM")
    return member


def _authorize(engine: Engine, context: DMGuidanceContext) -> None:
    with Session(engine) as session:
        _dm_member(session, context)


def assemble_guidance_context(
    session: Session,
    context: DMGuidanceContext,
    vector: list[float],
    *,
    max_results: int = 5,
    max_chars: int = 6000,
    per_chunk_chars: int = 1500,
) -> list[dict]:
    """Sync core: content budget excludes structured citation metadata.

    Each excerpt carries a complete source citation even when text is truncated.
    The bounded result list follows score then chunk id for deterministic ties.
    """
    if not (
        1 <= max_results <= 10
        and 1 <= max_chars <= 12000
        and 1 <= per_chunk_chars <= 3000
    ):
        raise ValueError("Guidance context bounds exceeded")
    member = _dm_member(session, context)
    hits = search_knowledge_vector(
        session,
        vector,
        context.campaign_id,
        "dm",
        member,
        max_results,
        for_context=True,
        topic="dm_guidance",
    )
    result = []
    remaining = max_chars
    for hit in hits:
        chunk = session.get(KnowledgeChunk, hit["id"], populate_existing=True)
        if chunk is None or chunk.topic != "dm_guidance":
            continue
        content = chunk.content[: min(remaining, per_chunk_chars)]
        if not content:
            break
        result.append(
            {
                "content": content,
                "source": {
                    "book_id": chunk.book_id,
                    "chunk_id": chunk.id,
                    "chunk_ref": chunk.chunk_ref,
                    "section_path": chunk.section_path,
                    "section_hierarchy": chunk.section_hierarchy,
                },
                "display_name": hit["display_name"],
                "topic": chunk.topic,
            }
        )
        remaining -= len(content)
    return result


def _assemble(
    engine: Engine, context: DMGuidanceContext, vector: list[float], bounds: dict
) -> list[dict]:
    with Session(engine) as session:
        return assemble_guidance_context(session, context, vector, **bounds)


async def guidance_context(
    engine: Engine,
    embed_client: _Embedder,
    context: DMGuidanceContext,
    q: str,
    **bounds,
) -> list[dict]:
    """Network seam plus fresh thread-owned sessions; no shared Session crosses threads."""
    await asyncio.to_thread(_authorize, engine, context)
    vector = await embed_client.embed(q)
    return await asyncio.to_thread(_assemble, engine, context, vector, bounds)
