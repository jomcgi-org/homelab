"""OpenRouter entity extraction: knowledge_chunk -> entities/mentions/relationships.

Spec #4.2 (docs/plans/2026-07-02-grimoire-pg-first-spec.md): a batch job body
that reads loaded chunks with no entity mentions yet, calls a frontier model
via OpenRouter for structured JSON extraction, and writes entities (spine +
typed detail per ADR 011), ``chunk_entity_mention`` rows, and
``relationship`` rows.

Dedup semantics (ADR 012, rev.): entities are deduped by ``(entity_type,
lower(name))``. If an entity with that key already exists, extraction reuses it
and *enriches* its typed detail: scalar columns are filled only where still
NULL and JSONB fields are key-merged with existing keys winning. So a monster
split across chunks (lore in one, stat block in another) ends up whole, while a
later chunk never clobbers a value an earlier one already set. This keeps the
job idempotent (a re-run fills nothing new) and needs no reconciliation pass.

Cache-key semantics: a chunk is considered processed for a given
``(model, prompt_hash)`` once a ``grimoire.chunk_extraction`` marker row
exists for that key (see ``models.ChunkExtraction``). Failure semantics: a
chunk that fails extraction (HTTP error after retries, malformed JSON, or a
shape that does not match the expected schema) gets no marker row, so it is
naturally re-selected and retried on the next run under the same key. A
chunk that genuinely contains no entities gets a marker with
``status="empty"`` so it is not re-run forever; changing the model or the
prompt (which changes ``prompt_hash``) makes every chunk pending again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Protocol

import httpx
from sqlalchemy import func
from sqlmodel import Session, select

from grimoire.ingest import upsert_embedding_batch
from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    ChunkEntityMention,
    ChunkExtraction,
    Entity,
    EntityCreature,
    EntityLocation,
    EntityNpc,
    EntitySpell,
    KnowledgeChunk,
    Relationship,
)

logger = logging.getLogger("monolith.grimoire.extract")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"

# A paid API behind a stable HTTP interface fails less often than a local
# model server, so fewer retries than shared/embedding.py's 12 is enough.
EXTRACT_MAX_RETRIES = 4
EXTRACT_RETRY_BASE_DELAY = 2.0  # seconds
EXTRACT_RETRY_MAX_DELAY = 20.0  # cap per-retry wait
EXTRACT_CONNECT_TIMEOUT = 5.0
EXTRACT_READ_TIMEOUT = 120.0  # frontier-model completions are slower than embeds

DEFAULT_LIMIT = 25
ENTITY_TYPES = {"creature", "spell", "location", "npc", "faction", "deity", "item"}

# Mirrors ingest._Embedder: a per-module Protocol so extract.py does not
# depend on ingest.py's private name across the package boundary.
EMBED_BATCH_SIZE = 64


class _Embedder(Protocol):
    model: str

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


EXTRACTION_PROMPT = """You are extracting structured game-lore data from a D&D 5e sourcebook \
text chunk. Read the chunk and emit ONLY a single strict JSON object, no prose, matching \
this shape:

{
  "entities": [
    {
      "entity_type": "creature|spell|location|npc|faction|deity|item",
      "name": "string",
      "summary": "1-2 sentence summary",
      "detail": { ... typed fields, see below, may be partial or omitted }
    }
  ],
  "mentions": [
    {"entity_name": "string", "mention_text": "short quoted or paraphrased context"}
  ],
  "relationships": [
    {"from_name": "string", "to_name": "string", "rel_type": "UPPER_SNAKE_CASE"}
  ]
}

Only include something in "entities" if the chunk genuinely *describes* it (stats, \
appearance, history, mechanics). A name that is only dropped in passing (e.g. "as told in \
the legends of Waterdeep") belongs in "mentions" or as the endpoint of a "relationships" \
edge, not in "entities".

"detail" field vocabulary by entity_type (field names match the typed detail table columns; \
include only fields you can support from the text, omit the rest, and omit "detail" entirely \
for faction/deity/item, which have no detail table):

- creature: size (str), creature_type (str), ac (int), hp_avg (int), cr (number, e.g. 0.5 or \
2), speed (object), ability_scores (object), actions (object), traits (object)
- spell: level (int, 0 for cantrip), school (str), casting_time (str), range (str), \
components (str), duration (str), classes (object), description (str)
- location: location_type (str), region (str), description (str)
- npc: race (str), occupation (str), disposition (str), description (str)

"relationships" rel_type examples: LOCATED_IN, MEMBER_OF, KNOWS, SERVES, RULES, ENEMY_OF, \
ALLY_OF. Use the entity's "name" (or a mentioned name) for from_name/to_name/entity_name so \
the same string can be resolved back to the entity it names.

If the chunk describes nothing extractable, return {"entities": [], "mentions": [], \
"relationships": []}."""


class OpenRouterError(Exception):
    """Raised when an OpenRouter call fails after exhausting retries."""


class _ContentParseError(ValueError):
    """Malformed or unexpected chat-completion content.

    A ValueError subclass (so ``except ValueError`` still catches it) that
    also carries the raw response text -- empty if the failure happened
    before any content could be extracted -- so ``extract`` can echo the
    bad output back to the model in a self-correction turn.
    """

    def __init__(self, message: str, raw_content: str = ""):
        super().__init__(message)
        self.raw_content = raw_content


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    """Best-effort removal of a leading/trailing markdown code fence that
    cheaper models sometimes wrap JSON in. Leaves clean JSON untouched."""
    return _FENCE_RE.sub("", text.strip())


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient errors worth retrying."""
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
            httpx.NetworkError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    return False


class OpenRouterClient:
    """Thin async client for OpenRouter's OpenAI-compatible chat completions."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str | None = None,
        base_url: str | None = None,
    ):
        self.api_key = api_key
        self.model = model or os.environ.get("GRIMOIRE_EXTRACT_MODEL", DEFAULT_MODEL)
        self.base_url = base_url or os.environ.get(
            "GRIMOIRE_EXTRACT_BASE_URL", OPENROUTER_URL
        )

    def _headers(self) -> dict:
        """Bearer auth header when an api_key is set, empty dict otherwise.

        Keyless in-cluster endpoints (e.g. local Qwen via vLLM) do not need
        or accept an Authorization header.
        """
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def extract(self, chunk_text: str) -> dict:
        """Extract structured JSON from one chunk of text.

        Retries with exponential backoff on transient HTTP errors inside
        ``_post_and_parse`` (connection failures, timeouts, 5xx); raises
        OpenRouterError once those are exhausted or on a non-retryable HTTP
        error. On a JSON parse failure (which is never retried at the HTTP
        layer, since a malformed completion will not fix itself), this
        makes exactly ONE follow-on self-correction call: the bad output
        and the parse error are echoed back to the model, asking for clean
        JSON only. If that second attempt also fails to parse, the failure
        propagates and the chunk is left pending for the next run. Local
        Qwen (vLLM guided JSON) rarely triggers this path; it earns its
        keep on hosted models that sometimes wrap JSON in prose or
        markdown fences.
        """
        messages = [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": chunk_text},
        ]
        try:
            _content, parsed = await self._post_and_parse(messages)
            return parsed
        except ValueError as first_err:
            bad_content = getattr(first_err, "raw_content", "")
            correction = messages + [
                {"role": "assistant", "content": bad_content},
                {
                    "role": "user",
                    "content": (
                        f"That did not parse as JSON ({first_err}). "
                        "Return only the JSON object, no prose or markdown."
                    ),
                },
            ]
            _content, parsed = await self._post_and_parse(correction)
            return parsed

    async def _post_and_parse(self, messages: list[dict]) -> tuple[str, dict]:
        """POST one chat-completion request built from ``messages`` and parse it.

        Retries with exponential backoff on transient HTTP errors
        (connection failures, timeouts, 5xx); raises OpenRouterError once
        retries are exhausted or on a non-retryable HTTP error. Raises
        ``_ContentParseError`` (a ValueError) if the response shape is
        unexpected or its message content is not valid JSON after
        stripping a markdown code fence; that carries the raw content so
        ``extract`` can echo it back in a self-correction turn.
        """
        timeout = httpx.Timeout(EXTRACT_READ_TIMEOUT, connect=EXTRACT_CONNECT_TIMEOUT)
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        headers = self._headers()

        last_exc: Exception | None = None
        for attempt in range(EXTRACT_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(  # nosemgrep: tainted-fastapi-http-request-httpx (self.base_url is a config value, not user input)
                        self.base_url,
                        json=payload,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    body = resp.json()
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt == EXTRACT_MAX_RETRIES - 1:
                    raise OpenRouterError(f"OpenRouter call failed: {exc}") from exc
                delay = min(
                    EXTRACT_RETRY_BASE_DELAY * (2**attempt), EXTRACT_RETRY_MAX_DELAY
                )
                logger.warning(
                    "OpenRouter call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    EXTRACT_MAX_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue
            return self._parse_content(body)

        raise OpenRouterError("OpenRouter call failed: exhausted retries") from last_exc

    @staticmethod
    def _parse_content(body: dict) -> tuple[str, dict]:
        """Extract and parse the message content from a chat-completion body.

        Applies ``_strip_fences`` before ``json.loads`` so a markdown-fenced
        JSON blob (common on hosted models) parses without needing a
        correction round-trip.
        """
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise _ContentParseError(
                f"unexpected OpenRouter response shape: {e}"
            ) from e
        cleaned = _strip_fences(content)
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError) as e:
            raise _ContentParseError(
                f"OpenRouter content is not valid JSON: {e}", raw_content=content
            ) from e
        if not isinstance(parsed, dict):
            raise _ContentParseError(
                "OpenRouter content JSON is not an object", raw_content=content
            )
        return content, parsed


# Detail field -> expected Python type, keyed by detail model. Mirrors
# EntityCreature/EntitySpell/EntityLocation/EntityNpc column types in
# models.py so the extraction detail-field vocabulary maps mechanically.
_DETAIL_FIELD_TYPES: dict[type, dict[str, type]] = {
    EntityCreature: {
        "size": str,
        "creature_type": str,
        "ac": int,
        "hp_avg": int,
        "cr": float,
        "speed": dict,
        "ability_scores": dict,
        "actions": dict,
        "traits": dict,
    },
    EntitySpell: {
        "level": int,
        "school": str,
        "casting_time": str,
        "range": str,
        "components": str,
        "duration": str,
        "classes": dict,
        "description": str,
    },
    EntityLocation: {"location_type": str, "region": str, "description": str},
    EntityNpc: {"race": str, "occupation": str, "disposition": str, "description": str},
}


def _coerce_detail_fields(
    detail_model: type, entity_name: str, detail: dict
) -> dict[str, Any]:
    """Coerce a raw detail payload to typed field kwargs (no entity_id).

    Any key in ``detail`` not in the model's known field set is silently
    ignored (defensive against the model inventing extra fields); a known
    field whose value cannot be coerced to the expected type is dropped
    with a warning rather than failing the whole chunk.
    """
    field_types = _DETAIL_FIELD_TYPES[detail_model]
    coerced: dict[str, Any] = {}
    for field_name, expected_type in field_types.items():
        if field_name not in detail:
            continue
        value = detail[field_name]
        if value is None:
            continue
        if expected_type is dict:
            if isinstance(value, dict):
                coerced[field_name] = value
            else:
                logger.warning(
                    "grimoire extract: dropping non-object detail field %s.%s for %r",
                    detail_model.__tablename__,
                    field_name,
                    entity_name,
                )
            continue
        if expected_type is str:
            coerced[field_name] = value if isinstance(value, str) else str(value)
            continue
        # int / float
        try:
            coerced[field_name] = expected_type(value)
        except (TypeError, ValueError):
            logger.warning(
                "grimoire extract: dropping uncoercible detail field %s.%s=%r for %r",
                detail_model.__tablename__,
                field_name,
                value,
                entity_name,
            )
    return coerced


def _create_or_enrich_detail(
    session: Session, detail_model: type, entity_id: str, entity_name: str, detail: dict
) -> None:
    """Insert the typed detail row, or enrich an existing one (ADR 012 rev.).

    Enrich, not overwrite: a scalar column is filled only when the stored value
    is still NULL, and a JSONB dict column is key-merged with existing keys
    winning. So a monster whose lore and stat block land in different chunks
    ends up whole, while a later chunk never clobbers an earlier one's value.
    """
    coerced = _coerce_detail_fields(detail_model, entity_name, detail)
    if not coerced:
        return
    existing = session.get(detail_model, entity_id)
    if existing is None:
        session.add(detail_model(entity_id=entity_id, **coerced))
        return
    field_types = _DETAIL_FIELD_TYPES[detail_model]
    for field_name, value in coerced.items():
        if field_types[field_name] is dict:
            if value:
                current = getattr(existing, field_name) or {}
                # Reassign a fresh dict (not in-place) so SQLAlchemy flags the
                # JSONB column dirty; existing keys win, new keys fill the gaps.
                setattr(existing, field_name, {**value, **current})
        elif getattr(existing, field_name) is None:
            setattr(existing, field_name, value)


def _get_or_create_entity(
    session: Session,
    chunk: KnowledgeChunk,
    item: dict,
    local_by_name: dict[str, Entity],
) -> tuple[Entity, bool] | None:
    """Resolve or create the Entity spine (+ detail row, if new) for one extracted entity.

    Returns ``(entity, created)`` or None if ``item`` is not a usable entity
    (missing/invalid entity_type or name).
    """
    entity_type = item.get("entity_type")
    name = item.get("name")
    if entity_type not in ENTITY_TYPES or not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    name_key = name.lower()

    existing = (
        session.execute(
            select(Entity).where(
                Entity.entity_type == entity_type,
                func.lower(Entity.name) == name_key,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        entity = existing
        created = False
        local_by_name.setdefault(name_key, existing)
    else:
        entity = Entity(
            entity_type=entity_type,
            name=name,
            source_type="extracted",
            is_global=True,
            source_book=chunk.book_id,
        )
        session.add(entity)
        session.flush()
        local_by_name[name_key] = entity
        created = True

    # Both paths enrich detail: a new entity's row is created, an existing one
    # is filled where NULL (so a monster split across chunks ends up whole).
    detail_model = ENTITY_DETAIL_MODELS.get(entity_type)
    detail = item.get("detail")
    if detail_model is not None and isinstance(detail, dict) and detail:
        _create_or_enrich_detail(session, detail_model, entity.id, name, detail)

    return entity, created


def _resolve_entity_name(
    session: Session, local_by_name: dict[str, Entity], name: str
) -> Entity | None:
    """Resolve a bare name to an Entity: this chunk's extraction first, then the DB.

    Name lookup is global (not scoped to a single entity_type), matching how
    mentions/relationships reference entities by name alone. If more than
    one entity_type shares a name, an arbitrary match is returned; this
    ambiguity is accepted for v1.
    """
    name_key = name.strip().lower()
    if not name_key:
        return None
    if name_key in local_by_name:
        return local_by_name[name_key]
    return (
        session.execute(select(Entity).where(func.lower(Entity.name) == name_key))
        .scalars()
        .first()
    )


def _insert_mention(
    session: Session, chunk_id: str, entity_id: str, mention_text: str | None
) -> bool:
    existing = (
        session.execute(
            select(ChunkEntityMention.chunk_id).where(
                ChunkEntityMention.chunk_id == chunk_id,
                ChunkEntityMention.entity_id == entity_id,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return False
    session.add(
        ChunkEntityMention(
            chunk_id=chunk_id, entity_id=entity_id, mention_text=mention_text
        )
    )
    return True


def _insert_relationship(
    session: Session, from_entity_id: str, to_entity_id: str, rel_type: str
) -> bool:
    existing = (
        session.execute(
            select(Relationship.id).where(
                Relationship.from_entity_id == from_entity_id,
                Relationship.to_entity_id == to_entity_id,
                Relationship.rel_type == rel_type,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return False
    session.add(
        Relationship(
            from_entity_id=from_entity_id, to_entity_id=to_entity_id, rel_type=rel_type
        )
    )
    return True


def _prompt_hash() -> str:
    """Stable cache-key component for the current EXTRACTION_PROMPT text."""
    return hashlib.sha256(EXTRACTION_PROMPT.encode("utf-8")).hexdigest()


def current_extraction_key() -> tuple[str, str]:
    """The ``(model, prompt_hash)`` marker key the extraction pass writes right
    now (env override or DEFAULT_MODEL, current prompt text).

    Read paths that report extraction coverage (grimoire.library) count
    ``chunk_extraction`` rows under exactly this key, so a book's "extracted"
    count reflects the live model+prompt: bumping either resets coverage to
    zero the same way it makes every chunk pending again in
    ``_select_pending_chunks``.
    """
    model = os.environ.get("GRIMOIRE_EXTRACT_MODEL", DEFAULT_MODEL)
    return model, _prompt_hash()


def _select_pending_chunks(
    session: Session, model: str, prompt_hash: str, limit: int
) -> list[KnowledgeChunk]:
    """Sync select of up to ``limit`` chunks with no marker yet, oldest first.

    A chunk is pending if there is no ``chunk_extraction`` row for THIS
    exact ``(model, prompt_hash)`` key; a different model or prompt makes
    every chunk pending again. Optional ``GRIMOIRE_EXTRACT_BOOK`` scopes
    selection to one ``book_id`` for staged eval runs. Ordered by
    created_at so ``limit`` is a deterministic FIFO cutoff, not whatever
    order the DB happens to return unprocessed rows in.
    """
    marker_exists = (
        select(ChunkExtraction.chunk_id)
        .where(
            ChunkExtraction.chunk_id == KnowledgeChunk.id,
            ChunkExtraction.model == model,
            ChunkExtraction.prompt_hash == prompt_hash,
        )
        .exists()
    )
    query = select(KnowledgeChunk).where(~marker_exists)
    book = os.environ.get("GRIMOIRE_EXTRACT_BOOK")
    if book:
        query = query.where(KnowledgeChunk.book_id == book)
    query = query.order_by(KnowledgeChunk.created_at).limit(limit)
    return list(session.execute(query).scalars().all())


def _apply_extraction(
    session: Session,
    chunk: KnowledgeChunk,
    extraction: dict,
    newly_created: list[tuple[Entity, str]],
) -> dict[str, int]:
    """Sync write of one chunk's parsed extraction inside its own savepoint.

    Isolated from ``extract_chunks`` (which is ``async def``) so no Session
    I/O runs written directly in an async function body, mirroring
    ``ingest.py``'s ``_upsert_book_chunks`` / ``upsert_embedding_batch``
    split. Mutates ``newly_created`` in place with (entity, embed_text) for
    every entity created this call; returns the per-chunk count deltas.
    """
    counts = {
        "entities_created": 0,
        "entities_reused": 0,
        "mentions_created": 0,
        "relationships_created": 0,
    }
    with session.begin_nested():
        local_by_name: dict[str, Entity] = {}

        for item in extraction["entities"]:
            if not isinstance(item, dict):
                continue
            result = _get_or_create_entity(session, chunk, item, local_by_name)
            if result is None:
                continue
            entity, created = result
            summary_text = item.get("summary")
            summary_text = summary_text if isinstance(summary_text, str) else ""
            if created:
                counts["entities_created"] += 1
                newly_created.append((entity, summary_text))
            else:
                counts["entities_reused"] += 1
            if _insert_mention(session, chunk.id, entity.id, summary_text or None):
                counts["mentions_created"] += 1

        for mention in extraction["mentions"]:
            if not isinstance(mention, dict):
                continue
            name = mention.get("entity_name")
            if not isinstance(name, str):
                continue
            entity = _resolve_entity_name(session, local_by_name, name)
            if entity is None:
                logger.debug(
                    "grimoire extract: chunk %s unresolvable mention name %r",
                    chunk.id,
                    name,
                )
                continue
            mention_text = mention.get("mention_text")
            mention_text = mention_text if isinstance(mention_text, str) else None
            if _insert_mention(session, chunk.id, entity.id, mention_text):
                counts["mentions_created"] += 1

        for rel in extraction["relationships"]:
            if not isinstance(rel, dict):
                continue
            from_name = rel.get("from_name")
            to_name = rel.get("to_name")
            rel_type = rel.get("rel_type")
            if (
                not isinstance(from_name, str)
                or not isinstance(to_name, str)
                or not isinstance(rel_type, str)
                or not rel_type
            ):
                continue
            from_entity = _resolve_entity_name(session, local_by_name, from_name)
            to_entity = _resolve_entity_name(session, local_by_name, to_name)
            if from_entity is None or to_entity is None:
                logger.debug(
                    "grimoire extract: chunk %s unresolvable relationship %r -> %r",
                    chunk.id,
                    from_name,
                    to_name,
                )
                continue
            if _insert_relationship(session, from_entity.id, to_entity.id, rel_type):
                counts["relationships_created"] += 1

    return counts


def _commit(session: Session) -> None:
    session.commit()


async def extract_chunks(
    session: Session,
    or_client: OpenRouterClient,
    embed_client: _Embedder,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Extract entities/mentions/relationships from up to ``limit`` unprocessed chunks.

    A chunk is "unprocessed" if it has no ``chunk_extraction`` marker row
    for this ``(model, prompt_hash)`` key yet (see module docstring). All
    Session I/O lives in the sync helpers above, called (not awaited)
    directly from this function's body, mirroring ``ingest.load_chunks``;
    the ``await`` calls here are only ``or_client.extract`` and
    ``embed_client.embed_batch``. Newly created entities are embedded (name
    + summary) in batches after the write loop, reusing
    ``ingest.upsert_embedding_batch``.

    Returns
    ``{"chunks_processed", "chunks_failed", "entities_created",
    "entities_reused", "mentions_created", "relationships_created",
    "entities_embedded"}``.
    """
    model = or_client.model
    prompt_hash = _prompt_hash()
    chunks = _select_pending_chunks(session, model, prompt_hash, limit)

    summary = {
        "chunks_processed": 0,
        "chunks_failed": 0,
        "entities_created": 0,
        "entities_reused": 0,
        "mentions_created": 0,
        "relationships_created": 0,
        "entities_embedded": 0,
    }
    # (entity, embed_text) for entities created this run; captured here
    # rather than read back from Entity (which has no summary column) since
    # the summary only exists transiently in the extraction payload.
    newly_created: list[tuple[Entity, str]] = []

    for chunk in chunks:
        try:
            extraction = await or_client.extract(chunk.content)
        except (OpenRouterError, ValueError) as exc:
            logger.warning(
                "grimoire extract: chunk %s failed extraction: %s", chunk.id, exc
            )
            summary["chunks_failed"] += 1
            continue

        entities = extraction.get("entities")
        mentions = extraction.get("mentions")
        relationships = extraction.get("relationships")
        if (
            not isinstance(entities, list)
            or not isinstance(mentions, list)
            or not isinstance(relationships, list)
        ):
            logger.warning(
                "grimoire extract: chunk %s extraction shape invalid", chunk.id
            )
            summary["chunks_failed"] += 1
            continue

        counts = _apply_extraction(
            session,
            chunk,
            {
                "entities": entities,
                "mentions": mentions,
                "relationships": relationships,
            },
            newly_created,
        )
        for key, value in counts.items():
            summary[key] += value
        summary["chunks_processed"] += 1

        status = (
            "ok"
            if counts["entities_created"]
            or counts["entities_reused"]
            or counts["mentions_created"]
            else "empty"
        )
        with session.begin_nested():
            session.add(
                ChunkExtraction(
                    chunk_id=chunk.id,
                    model=model,
                    prompt_hash=prompt_hash,
                    status=status,
                )
            )

    _commit(session)

    for start in range(0, len(newly_created), EMBED_BATCH_SIZE):
        batch = newly_created[start : start + EMBED_BATCH_SIZE]
        texts = [f"{entity.name}: {summary_text}" for entity, summary_text in batch]
        vectors = await embed_client.embed_batch(texts)
        entities_only = [entity for entity, _ in batch]
        summary["entities_embedded"] += upsert_embedding_batch(
            session, embed_client.model, "entity", entities_only, vectors
        )

    logger.info("grimoire extract: %s", summary)
    return summary
