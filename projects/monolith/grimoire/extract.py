"""OpenRouter entity extraction: knowledge_chunk -> entities/mentions/relationships.

Spec #4.2 of the pg-first design: a batch job body
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

Locations are the one exception (they alone name rooms, and the same room name
recurs across dungeons and books): a ``location`` is deduped within its own
``source_book`` and by a ``site`` key, the parent place of the room's keyed
entry (see ``_room_site``). Two same-named rooms under different sites, or in
different books, stay separate nodes; a bare same-book reference that names no
site still resolves to the book's single candidate, but an ambiguous one never
guesses. ``table`` (v6) is deduped within ``source_book`` too but WITHOUT the
site logic: table captions ("Random Encounters") repeat across books, so a
same-named table in another book stays separate, while a repeat within one book
(a table split across chunks) reuses the oldest candidate and key-merges its
rows. Every other entity type keeps the global ``(entity_type, lower(name))``
dedup unchanged.

Cache-key semantics: a chunk is considered processed for a given
``(model, prompt_version)`` once a ``grimoire.chunk_extraction`` marker row
exists for that key (see ``models.ChunkExtraction``). The key uses the prompt
VERSION LABEL (``v1``, ``v2``, ...) rather than a sha256 of the prompt text, so
promoting a new prompt is an intentional act (bump ``PROMPT_VERSIONS`` and the
``GRIMOIRE_PROMPT_VERSION`` pointer) rather than an accidental byte-diff. A
frozen-hash unit test pins each released version's text, so editing a shipped
prompt fails CI and forces a new version label. Failure semantics: a chunk that
fails extraction (HTTP error after retries, malformed JSON, or a shape that does
not match the expected schema) gets no marker row, so it is naturally
re-selected and retried on the next run under the same key. A chunk that
genuinely contains no entities gets a marker with ``status="empty"`` so it is
not re-run forever; changing the model or the prompt version makes every chunk
pending again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import func
from sqlmodel import Session, select

from grimoire.ingest import upsert_embedding_batch
from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    Book,
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
import shared.inference

logger = logging.getLogger("monolith.grimoire.extract")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Production re-extraction default (spec #5): DeepSeek V4 Flash via OpenRouter is
# cheap, fast, and honors json_schema; reasoning is disabled per call so it does
# not spend reasoning tokens on a structured-extraction task. Overridable per
# environment via GRIMOIRE_EXTRACT_MODEL (deploy points production here; local
# Qwen stays selectable via GRIMOIRE_EXTRACT_MODEL/GRIMOIRE_EXTRACT_BASE_URL).
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

# A paid API behind a stable HTTP interface fails less often than a local
# model server, so fewer retries than shared/embedding.py's 12 is enough.
EXTRACT_MAX_RETRIES = 4
EXTRACT_RETRY_BASE_DELAY = 2.0  # seconds
EXTRACT_RETRY_MAX_DELAY = 20.0  # cap per-retry wait
EXTRACT_CONNECT_TIMEOUT = 5.0
EXTRACT_READ_TIMEOUT = 120.0  # frontier-model completions are slower than embeds

DEFAULT_LIMIT = 25
# Concurrent in-flight extract calls. The per-chunk work is GPU inference on
# the shared vLLM server, and a sequential (one-at-a-time) client starves
# vLLM's continuous batcher, so the GPU runs at batch size 1 (its worst case).
# Firing several requests concurrently lets vLLM batch them. Kept BELOW the
# server's --max-num-seqs (8) so this bulk job leaves decode-slot headroom for
# latency-sensitive trusted callers (public chat is separately hard-capped at 2
# by chat_public.limits; Discord / private chat / agents share the rest). It
# also bounds the group size for the incremental per-group commit below.
DEFAULT_CONCURRENCY = 6

# v4 generic typed-extraction taxonomy (spec v4), widened to 19 types by v6's
# ``table``. Grouped by the DERIVED category: lore (unchanged from v1), gameplay
# (event/quest), and mechanics (D&D game rules + the v6 ``table`` container).
# ``category`` itself is a stored generated column on the entity spine
# (models._ENTITY_CATEGORY_EXPR / the migration), not a value the extractor emits.
# ENTITY_TYPES is the union: it gates the spine (unknown types are dropped in
# _get_or_create_entity) and feeds the json_schema entity_type enum.
LORE_TYPES: frozenset[str] = frozenset(
    {"creature", "spell", "location", "npc", "faction", "deity", "item"}
)
GAMEPLAY_TYPES: frozenset[str] = frozenset({"event", "quest"})
MECHANICS_TYPES: frozenset[str] = frozenset(
    {
        "condition",
        "feat",
        "race",
        "background",
        "class",
        "subclass",
        "class_feature",
        "action",
        "rule",
        # v6: a table/list of options captured as ONE entity (a treasure/magic-item
        # table, spell list, class progression, random-encounter table) instead of
        # N junk row entities. Book-scoped dedup in _get_or_create_entity.
        "table",
    }
)
ENTITY_TYPES = set(LORE_TYPES | GAMEPLAY_TYPES | MECHANICS_TYPES)
# temporality is set only for these two gameplay types (nullable elsewhere).
TEMPORAL_TYPES: frozenset[str] = GAMEPLAY_TYPES
TEMPORALITY_VALUES: frozenset[str] = frozenset({"historical", "present", "future"})

# Mirrors ingest._Embedder: a per-module Protocol so extract.py does not
# depend on ingest.py's private name across the package boundary.
EMBED_BATCH_SIZE = 64


class _Embedder(Protocol):
    model: str

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


# Closed relationship vocabulary (spec #3 / proposal §3). 31 canonical types
# consolidate the 483 free-text rel_types observed in v1 (61% of which were
# singletons). Ordered by group for the prompt; the frozenset drives the
# post-parse safety net and the json_schema enum. Any rel_type the model emits
# outside this set is mapped to RELATED_TO (never dropped for that reason).
REL_TYPES: tuple[str, ...] = (
    # Spatial
    "LOCATED_IN",
    "CONTAINS",
    "NEAR",
    "CONNECTS_TO",
    "PART_OF",
    # Social / organizational
    "MEMBER_OF",
    "LEADER_OF",
    "SERVES",
    "ALLY_OF",
    "ENEMY_OF",
    "FOUNDED",
    # Kinship
    "PARENT_OF",
    "CHILD_OF",
    "SIBLING_OF",
    "ANCESTOR_OF",
    "DESCENDANT_OF",
    "SPOUSE_OF",
    # Religion
    "WORSHIPS",
    "PATRON_OF",
    # Creation
    "CREATED_BY",
    "CREATES",
    # Magic
    "CASTS",
    "GRANTS",
    "SUMMONS",
    "TRANSFORMS_INTO",
    "COUNTERED_BY",
    # Possession
    "OWNS",
    "WIELDS",
    # Taxonomy
    "VARIANT_OF",
    "ORIGINATES_FROM",
    # Events / gameplay (v4)
    "OCCURRED_AT",
    "INVOLVED",
    "PRECEDED",
    "CAUSED",
    "ADVANCES",
    "GIVEN_BY",
    "OBJECTIVE_AT",
    "REWARDS",
    # Mechanics (v4)
    "SUBCLASS_OF",
    "FEATURE_OF",
    "REQUIRES",
    # Fallback (escape hatch so the model never invents outside the set)
    "RELATED_TO",
)
REL_TYPE_SET: frozenset[str] = frozenset(REL_TYPES)
RELATED_TO = "RELATED_TO"

# Entity-type groups used by the relationship type-signature validator (spec #2).
# "agent" is anything that can act (npc/creature/faction/deity); "place" is a
# location. Kinship connects only living/divine agents (not factions).
AGENT_TYPES: frozenset[str] = frozenset({"npc", "creature", "faction", "deity"})
PLACE_TYPES: frozenset[str] = frozenset({"location"})
_KIN_TYPES: frozenset[str] = frozenset({"npc", "creature", "deity"})
# Gameplay / mechanics endpoint groups for the v4 relationship signatures.
EVENT_TYPES: frozenset[str] = frozenset({"event"})
QUEST_TYPES: frozenset[str] = frozenset({"quest"})
_ALL_TYPES: frozenset[str] = frozenset(ENTITY_TYPES)


def _pairs(
    from_types: frozenset[str] | set[str], to_types: frozenset[str] | set[str]
) -> frozenset[tuple[str, str]]:
    """Cartesian product of two entity-type groups as a set of (from, to) pairs."""
    return frozenset((f, t) for f in from_types for t in to_types)


@dataclass(frozen=True)
class RelSignature:
    """The type signature for one rel_type: the set of allowed concrete
    ``(from_type, to_type)`` endpoint pairs, a ``symmetric`` flag (order does not
    matter, so a "reversed" edge is never swapped), and a ``spatial`` flag.

    ``spatial`` marks the containment relations (LOCATED_IN / CONTAINS / PART_OF /
    NEAR / CONNECTS_TO). Spatial edges are NOT reordered on a direction mismatch,
    because the text can describe a magical exception (a location inside an item,
    a pocket dimension); the one narrow exception is handled explicitly in
    ``_apply_rel_signature``.
    """

    allowed: frozenset[tuple[str, str]]
    symmetric: bool = False
    spatial: bool = False


# Deterministic relationship type signatures (spec #2). Keyed by rel_type; only
# the rel_types with a meaningful type constraint are listed. A rel_type absent
# here (GRANTS, SUMMONS, TRANSFORMS_INTO, COUNTERED_BY, PATRON_OF) is not
# validated and is kept verbatim. RELATED_TO is any->any so it is always valid
# (the downgrade target never re-triggers).
REL_SIGNATURES: dict[str, RelSignature] = {
    # Spatial (magical-exception-aware: not reordered except place LOCATED_IN agent)
    "LOCATED_IN": RelSignature(_pairs(_ALL_TYPES, PLACE_TYPES), spatial=True),
    "CONTAINS": RelSignature(_pairs(PLACE_TYPES, _ALL_TYPES), spatial=True),
    "PART_OF": RelSignature(
        _pairs(PLACE_TYPES, PLACE_TYPES)
        | _pairs({"faction"}, {"faction"})
        # v4: a sub-event/sub-quest is PART_OF its parent (same-type; spatial flag
        # keeps it from being reordered, which is harmless for same-type endpoints).
        | _pairs(EVENT_TYPES, EVENT_TYPES)
        | _pairs(QUEST_TYPES, QUEST_TYPES),
        spatial=True,
    ),
    "NEAR": RelSignature(
        _pairs(PLACE_TYPES, PLACE_TYPES), symmetric=True, spatial=True
    ),
    "CONNECTS_TO": RelSignature(
        _pairs(PLACE_TYPES, PLACE_TYPES), symmetric=True, spatial=True
    ),
    # Social / organizational
    "MEMBER_OF": RelSignature(_pairs(AGENT_TYPES, {"faction"})),
    "FOUNDED": RelSignature(_pairs(AGENT_TYPES, {"faction"})),
    "LEADER_OF": RelSignature(_pairs(_KIN_TYPES, {"faction"})),
    "SERVES": RelSignature(_pairs(AGENT_TYPES, {"npc", "faction", "deity"})),
    "ALLY_OF": RelSignature(_pairs(AGENT_TYPES, AGENT_TYPES), symmetric=True),
    "ENEMY_OF": RelSignature(_pairs(AGENT_TYPES, AGENT_TYPES), symmetric=True),
    "WORSHIPS": RelSignature(_pairs(AGENT_TYPES, {"deity"})),
    # Creation (thing <-> creator; asymmetric so a backwards edge swaps)
    "CREATED_BY": RelSignature(_pairs(_ALL_TYPES, AGENT_TYPES)),
    "CREATES": RelSignature(_pairs(AGENT_TYPES, _ALL_TYPES)),
    # Magic / possession
    "CASTS": RelSignature(_pairs({"npc", "creature"}, {"spell"})),
    "OWNS": RelSignature(_pairs({"npc", "creature"}, {"item"})),
    "WIELDS": RelSignature(_pairs({"npc", "creature"}, {"item"})),
    # Taxonomy
    "VARIANT_OF": RelSignature(frozenset((t, t) for t in _ALL_TYPES), symmetric=True),
    "ORIGINATES_FROM": RelSignature(
        _pairs({"creature", "npc", "faction", "item"}, {"location", "faction"})
    ),
    # Kinship (npc/creature/deity on both ends). PARENT_OF/CHILD_OF/ANCESTOR_OF/
    # DESCENDANT_OF are asymmetric, but same-type endpoints mean the type signature
    # cannot detect a reversed edge; it only catches a kin edge pointed at a place.
    "PARENT_OF": RelSignature(_pairs(_KIN_TYPES, _KIN_TYPES)),
    "CHILD_OF": RelSignature(_pairs(_KIN_TYPES, _KIN_TYPES)),
    "ANCESTOR_OF": RelSignature(_pairs(_KIN_TYPES, _KIN_TYPES)),
    "DESCENDANT_OF": RelSignature(_pairs(_KIN_TYPES, _KIN_TYPES)),
    "SIBLING_OF": RelSignature(_pairs(_KIN_TYPES, _KIN_TYPES), symmetric=True),
    "SPOUSE_OF": RelSignature(_pairs(_KIN_TYPES, _KIN_TYPES), symmetric=True),
    # Events / gameplay (v4; all asymmetric, so a backwards edge swaps and an
    # impossible one downgrades to RELATED_TO, exactly like the rest).
    "OCCURRED_AT": RelSignature(_pairs(EVENT_TYPES, PLACE_TYPES)),
    "INVOLVED": RelSignature(_pairs(EVENT_TYPES, AGENT_TYPES)),
    # event -> event: same-type endpoints, so a reversed edge cannot be detected
    # by type alone (like kinship); an off-type endpoint still downgrades.
    "PRECEDED": RelSignature(_pairs(EVENT_TYPES, EVENT_TYPES)),
    "CAUSED": RelSignature(_pairs(EVENT_TYPES, EVENT_TYPES)),
    "ADVANCES": RelSignature(_pairs(EVENT_TYPES, QUEST_TYPES)),
    "GIVEN_BY": RelSignature(_pairs(QUEST_TYPES, {"npc"})),
    "OBJECTIVE_AT": RelSignature(_pairs(QUEST_TYPES, PLACE_TYPES)),
    "REWARDS": RelSignature(_pairs(QUEST_TYPES, {"item"})),
    # Mechanics (v4; asymmetric).
    "SUBCLASS_OF": RelSignature(_pairs({"subclass"}, {"class"})),
    "FEATURE_OF": RelSignature(_pairs({"class_feature"}, {"class", "subclass"})),
    "REQUIRES": RelSignature(_pairs({"feat"}, {"feat", "class", "race"})),
    # Fallback: any <-> any, so a downgraded edge is always valid.
    "RELATED_TO": RelSignature(_pairs(_ALL_TYPES, _ALL_TYPES), symmetric=True),
}


def _apply_rel_signature(
    from_type: str, to_type: str, rel_type: str
) -> tuple[str, bool, str | None]:
    """Graduated, magical-exception-aware type-signature check for one resolved edge.

    Returns ``(rel_type, swap, action)`` where ``swap`` asks the caller to
    exchange the from/to endpoints and ``action`` is ``None`` (kept), ``"swap"``,
    or ``"downgrade"`` (for the per-rel_type compliance monitor). Rules:

    - valid signature -> keep.
    - spatial rel (LOCATED_IN/CONTAINS/PART_OF/NEAR/CONNECTS_TO): NOT reordered on
      a direction mismatch (the text may describe a magical exception, e.g. a
      location inside an item), EXCEPT the narrow safe case ``place LOCATED_IN
      agent`` -> swap to ``agent LOCATED_IN place``. A ``location LOCATED_IN item``
      is left as-is.
    - non-spatial, reversed valid, asymmetric -> swap the endpoints.
    - non-spatial, invalid in both directions (e.g. LEADER_OF/SERVES/OWNS with a
      location endpoint) -> downgrade rel_type to RELATED_TO (the edge is kept,
      never dropped).
    """
    sig = REL_SIGNATURES.get(rel_type)
    if sig is None or (from_type, to_type) in sig.allowed:
        return rel_type, False, None
    if sig.spatial:
        if (
            rel_type == "LOCATED_IN"
            and from_type in PLACE_TYPES
            and to_type in AGENT_TYPES
        ):
            return rel_type, True, "swap"
        return rel_type, False, None
    if not sig.symmetric and (to_type, from_type) in sig.allowed:
        return rel_type, True, "swap"
    return RELATED_TO, False, "downgrade"


# Strict JSON schema (spec #4) sent as ``response_format`` (hosted) or
# ``guided_json`` (vLLM). Its leverage is the ``rel_type``/``entity_type`` enums:
# they make the closed vocabulary a hard decode-time constraint, not a prompt
# suggestion. ``detail`` stays an open object (typed fields are model-appropriate
# and optional). The post-parse safety net still runs, since some OpenRouter
# providers silently downgrade json_schema to json_object.
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    # v4: set only for event/quest; enum-constrained but optional.
                    "temporality": {
                        "type": "string",
                        "enum": sorted(TEMPORALITY_VALUES),
                    },
                    "detail": {"type": "object"},
                },
                "required": ["entity_type", "name", "summary"],
            },
        },
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string"},
                    "mention_text": {"type": "string"},
                },
                "required": ["entity_name", "mention_text"],
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from_name": {"type": "string"},
                    "to_name": {"type": "string"},
                    "rel_type": {"type": "string", "enum": list(REL_TYPES)},
                },
                "required": ["from_name", "to_name", "rel_type"],
            },
        },
    },
    "required": ["entities", "mentions", "relationships"],
}


# Curated book genre (spec #2 book-context plumbing). The genre drives two defect
# categories: the stat-block split (a monster TYPE with a stat block is a valid
# entity in a bestiary but furniture in an adventure) and class-feature-not-spell
# (rulebook class chapters). Fed into the extraction user message as
# "Book: {title} ({kind})". Unmapped books get title-only context (no guessed
# kind). Keyed by the ``book_id`` slug (the S3 path segment).
BOOK_KIND: dict[str, str] = {
    "monster-manual": "bestiary",
    "players-handbook-2024": "rulebook",
    "dungeon-masters-guide-2024": "rulebook",
    "deep-magic-5e": "spellbook",
    "vault-of-magic": "magic-items",
    "explorers-guide-to-wildemount": "setting-guide",
    "sword-coast-adventurers-guide": "setting-guide",
    "curse-of-strahd": "adventure",
    "lost-mine-of-phandelver": "adventure",
    "rime-of-the-frostmaiden": "adventure",
    "storm-kings-thunder": "adventure",
    "waterdeep-dragon-heist": "adventure",
    "planescape-adventures-in-the-multiverse": "adventure",
    "candlekeep-mysteries": "adventure-anthology",
    "tales-from-the-yawning-portal": "adventure-anthology",
    "keys-from-the-golden-vault": "adventure-anthology",
    "ghosts-of-saltmarsh": "adventure-anthology",
    "tomb-of-annihilation": "adventure",
    "descent-into-avernus": "adventure",
    "the-wild-beyond-the-witchlight": "adventure",
    "waterdeep-dungeon-of-the-mad-mage": "adventure",
    # Dragon/giant compendia share Mordenkainen's lore + player options +
    # bestiary shape; the bestiary stat-block rule only fires on chunks that
    # actually are stat-block entries, so it is safe for their mixed chapters.
    "fizbans-treasury-of-dragons": "bestiary",
    "bigby-presents-glory-of-giants": "bestiary",
    # Majority lore/locations/items around the Deck of Many Things; the
    # bestiary appendix is a minority of the book.
    "the-book-of-many-things": "setting-guide",
}
# The two book_kind values the structural adventure layer (grimoire.adventure)
# operates over: single-adventure books and multi-adventure anthologies. Used
# to scope which books get classified into adventures, not for extraction.
ADVENTURE_BOOK_KINDS = frozenset({"adventure", "adventure-anthology"})
# Prefix families for slug variants (edition suffixes / multi-volume sets), tried
# only after an exact BOOK_KIND miss. Conservative: only well-known families.
_BOOK_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("tome-of-beasts", "bestiary"),
    ("mordenkainen", "bestiary"),
    ("xanthars", "rulebook"),
    ("tashas", "rulebook"),
    ("system-reference", "rulebook"),
    ("eberron", "setting-guide"),
    ("sword-coast", "setting-guide"),
)


def book_kind(book_id: str) -> str | None:
    """Curated genre for a book slug, or None when unmapped (title-only context).

    Exact ``BOOK_KIND`` match first, then a known prefix family; never guesses a
    genre for an unrecognized slug.
    """
    kind = BOOK_KIND.get(book_id)
    if kind is not None:
        return kind
    for prefix, k in _BOOK_KIND_PREFIXES:
        if book_id.startswith(prefix):
            return k
    return None


# --- Prompt versioning registry (spec #1) ----------------------------------
# The extraction prompt is versioned by an explicit label. The marker key stores
# the label (v1, v2, ...), so promoting a prompt is a deliberate pointer move
# (GRIMOIRE_PROMPT_VERSION), not an accidental byte-diff. A frozen-hash test pins
# each released version's text; editing a shipped prompt fails CI and forces a
# new version. Iterate a candidate on a fresh book with GRIMOIRE_PROMPT_VERSION=v3;
# promote by moving ACTIVE_PROMPT_VERSION once the eval clears.


@dataclass(frozen=True)
class PromptVersion:
    """One released extraction prompt: the system text plus the optional strict
    JSON schema sent alongside it. ``schema=None`` keeps the legacy
    ``json_object`` format (v1); a schema turns on enum-constrained decoding."""

    text: str
    schema: dict[str, Any] | None


# v1: the original free-vocabulary prompt (kept verbatim so historical markers
# and the frozen-hash test remain meaningful). Do NOT edit this text; it is a
# released version. New guidance goes in a new PromptVersion.
_V1_PROMPT_TEXT = """You are extracting structured game-lore data from a D&D 5e sourcebook \
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


# v2: defect-informed rewrite (spec #3). Root causes addressed alongside the
# code: section-heading context is plumbed in the user message (fixes the +1
# Plate Armor / OCR / map-key defects at the source), the rel_type vocabulary is
# closed (fixes 483-type fragmentation), entity inclusion is the "statblock
# split" rule (drop generic gear / rules text / mechanical descriptors), class
# features are not spells, and naming is heading-anchored, map-key-stripped, and
# OCR-corrected via the heading spelling. Do NOT edit this text once released;
# add a v3 instead.
_V2_PROMPT_TEXT = """You extract a structured knowledge graph from one text chunk of a \
Dungeons & Dragons 5e sourcebook. The user message gives you the book and its genre \
(after "Book:"), the section heading (after "Section:"), and then the chunk body. Read \
them together and emit ONLY a single strict JSON object, no prose and no markdown fences, \
with exactly these three keys:

{
  "entities": [
    {
      "entity_type": "creature|spell|location|npc|faction|deity|item",
      "name": "Canonical Name",
      "summary": "1-2 sentence description grounded in THIS chunk",
      "detail": { ...typed fields, see below; may be partial or omitted }
    }
  ],
  "mentions": [
    {"entity_name": "Canonical Name", "mention_text": "short quoted or paraphrased context"}
  ],
  "relationships": [
    {"from_name": "Canonical Name", "to_name": "Canonical Name", "rel_type": "ONE_OF_THE_CLOSED_SET"}
  ]
}

USE THE BOOK GENRE
The Book: line names the genre; let it decide the "stat-block split":
- In a bestiary / monster manual / spellbook / magic-item catalog, the STAT-BLOCKED \
  SUBJECT of this entry IS a valid entity even though it is a type: "Brass Dragon", \
  "Drow Mage", "Fireball", "Sun Blade" are the catalog's named contents. Extract the \
  entry's own subject.
- In an adventure module or setting guide, extract the named individuals, places, \
  factions, and unique items of that story; a generic monster or gear type dropped into \
  a scene ("two goblins attack") is furniture, not an entity.
- In a rulebook, most chapters are rules text (return empty); a class chapter's features \
  are rules, never spells (see below).

USE THE SECTION PATH
The Section: line is a document breadcrumb from the outermost chapter down to this \
chunk's own heading, joined by " > " (e.g. "Chapter 4: Barovia > Village of Barovia > \
L17. Surgery"). Use the nesting: an ancestor place usually CONTAINS its descendants and a \
descendant is LOCATED_IN or PART_OF its ancestor (a room inside a building inside a \
region), and the ancestors disambiguate a generic leaf heading ("Area 5", "The Cellar") \
by the named place above it. Still anchor each entity's NAME to its own leaf heading (the \
last breadcrumb segment), not to an ancestor.

WHAT COUNTS AS AN ENTITY (the "stat-block split" test)
Extract an entity when it is a SPECIFIC, NAMED thing in the game world: either the \
stat-blocked SUBJECT of THIS chunk (guided by the book genre above) or a proper named \
fixture of the setting (a named monster, character, place, spell, organization, god, or \
magic item). "Strahd", "Waterdeep", "Wind Dukes of Aaqa" all pass.

DO NOT extract as entities (the most common v1 mistakes):
- A generic type merely MENTIONED in passing, not the subject of this chunk's stat block: \
  the "gargoyles" a monster fights, the "two goblins" in an encounter. Put it in \
  "mentions" or a relationship endpoint, or skip it. (A NAMED individual, "Klarg", \
  "Old Gnawbone", IS an entity.)
- Generic mundane gear with no proper name: "Quiver", "Spear", "shark-tooth necklace", \
  "a potion", "gold coins", or the plain base of a magic item ("+1 plate armor" with no \
  unique name). Mundane equipment is furniture, not lore.
- Rules, mechanics, hazards, or glossary text: casting rules, condition definitions, \
  ability checks, XP tables, a hazard stat block such as "Webs", or a skill/condition \
  glossary entry. If the chunk is pure rules, return all three lists empty.
- A mechanical descriptor used as the name. Use the proper name from the section heading, \
  never a mechanical descriptor, as the entity name.
If a name is only dropped in passing ("as told in the legends of Waterdeep") and not \
described here, put it in "mentions" or as a relationship endpoint, NOT in "entities".

CLASS FEATURES ARE NOT SPELLS
In a rulebook's class chapter, a feature gained at a class or subclass level (for example \
"Thought Shield", "Spell Breaker") is rules text, NOT a spell. Never emit a class feature \
with entity_type "spell". If it is not clearly a named entity in its own right, omit it \
entirely.

CANONICAL NAMING (critical: the graph deduplicates entities by exact name, so \
inconsistent naming splits one entity into several broken fragments)
- Anchor the name to the section heading's proper noun. The heading is the most reliable \
  spelling; prefer it over body text and especially over image/OCR text. If the body OCR \
  reads "Rheded" but the heading says "REGHED BARBARIANS", the name is "Reghed". If the \
  body reads "Wyrling" but the heading says "GREEN DRAGON WYRMLING", use "Wyrmling".
- STRIP a map-key or catalog prefix from the heading: "L17. Surgery" -> "Surgery", \
  "P2. West Shore" -> "West Shore", "17. Old Mill" -> "Old Mill".
- Use Title Case, NOT the all-caps of a stat-block header: "Lifedrinker", not "LIFEDRINKER".
- DROP a leading article: "Zhentarim", not "The Zhentarim"; "Nine Hells", not "The Nine Hells".
- Use the SINGULAR form for a kind of creature or item: "Gargoyle", not "Gargoyles".
- Use straight punctuation matching the most common spelling: "Uk'otoa", not the \
  curly-quote form; "Zhentarim", not "Zhen-tarim".
- Drop honorifics/epithets unless inseparable from the name ("Strahd", not \
  "Strahd von Zarovich, the Ancient"; but keep "Dendar the Night Serpent" if that is the \
  consistent name). Prefer the shortest unambiguous canonical form.
- Use the SAME string for an entity's own name, its mentions, and every relationship \
  endpoint that refers to it, so they resolve to one node.

RELATIONSHIPS: use ONLY these rel_type values (closed set). Pick the closest match; if \
none fits, use RELATED_TO. Never invent a new type, never pluralize, never add a typo'd \
or directional variant.
  Spatial:      LOCATED_IN, CONTAINS, NEAR, CONNECTS_TO, PART_OF
  Social/org:   MEMBER_OF, LEADER_OF, SERVES, ALLY_OF, ENEMY_OF, FOUNDED
  Kinship:      PARENT_OF, CHILD_OF, SIBLING_OF, ANCESTOR_OF, DESCENDANT_OF, SPOUSE_OF
  Religion:     WORSHIPS, PATRON_OF
  Creation:     CREATED_BY, CREATES
  Magic:        CASTS, GRANTS, SUMMONS, TRANSFORMS_INTO, COUNTERED_BY
  Possession:   OWNS, WIELDS
  Taxonomy:     VARIANT_OF, ORIGINATES_FROM
  Fallback:     RELATED_TO
Endpoint-type and direction rules (a wrong-typed edge is worse than no edge):
- ENEMY_OF, ALLY_OF, SERVES, MEMBER_OF, LEADER_OF connect two AGENTS (npc, faction, \
  creature, or deity). Never point one of these at a location. "Ki'Nau ENEMY_OF Lushgut \
  Forest" and "Suljack SERVES Luskan" are wrong: use LOCATED_IN or LEADER_OF against the \
  place's ruling faction instead.
- CASTS and GRANTS come only from a class, subclass, feature, creature, or item, never \
  from a faction. "Circle of the Arctic GRANTS Cone of Cold" is wrong.
- Direction: container/owner/wielder is the FROM endpoint. CONTAINS (place -> thing), \
  OWNS / WIELDS (owner -> owned). "Amulet CONTAINS the wizard" is backwards.
- Map common phrasings: "rules/governs/commands/heads" -> LEADER_OF; "resides in/lives \
  in/based in" -> LOCATED_IN; "allied with/works with" -> ALLY_OF; "rival of/opposes/ \
  hunts" -> ENEMY_OF; "forged by/crafted by/built by" -> CREATED_BY; "worships/venerates" \
  -> WORSHIPS; "subtype of/is a kind of/form of" -> VARIANT_OF.
- Only assert an edge when BOTH endpoints are real named entities you also list in \
  "entities" or "mentions". An edge to a generic type ("ENEMY_OF Goblin") or an unnamed \
  thing is discarded, so do not emit it.
- Extract EVERY meaningful connection the chunk supports (who serves whom, what is where, \
  who made what, family, allegiance, worship) inside the closed vocabulary above.

DETAIL fields by entity_type (field names match the DB columns; include only what the \
text supports, omit the rest; omit "detail" entirely for faction/deity/item):
- creature: size(str), creature_type(str), ac(int), hp_avg(int), cr(number, e.g. 0.5 or 2), \
  speed(object), ability_scores(object), actions(object), traits(object)
- spell: level(int, 0 for cantrip), school(str), casting_time(str), range(str), \
  components(str), duration(str), classes(object), description(str)
- location: location_type(str), region(str), description(str)
- npc: race(str), occupation(str), disposition(str), description(str)

If the chunk describes nothing extractable (pure rules, a table, flavor with no named \
entity), return {"entities": [], "mentions": [], "relationships": []}.

EXAMPLES

Example 1 - bestiary entry: the stat-blocked subject is a valid entity
Input:
Book: Monster Manual (bestiary)
Section: AARAKOCRA

Aarakocra range the Howling Gyre, an endless storm that surrounds the tranquil realm of \
Aaqa in the Elemental Plane of Air. These birdlike humanoids guard the windy borders of \
their home against invaders from the Elemental Plane of Earth, such as gargoyles, their \
sworn enemies. In service to the Wind Dukes of Aaqa, aarakocra scout the planes in search \
of temples of Elemental Evil.
Output:
{"entities":[
  {"entity_type":"creature","name":"Aarakocra","summary":"Birdlike humanoids of the Elemental Plane of Air who patrol the Howling Gyre and serve the Wind Dukes of Aaqa against elemental incursions.","detail":{"creature_type":"humanoid"}},
  {"entity_type":"location","name":"Howling Gyre","summary":"An endless storm surrounding the realm of Aaqa in the Elemental Plane of Air.","detail":{"location_type":"storm","region":"Elemental Plane of Air"}},
  {"entity_type":"location","name":"Aaqa","summary":"A tranquil realm in the Elemental Plane of Air, encircled by the Howling Gyre.","detail":{"location_type":"realm","region":"Elemental Plane of Air"}},
  {"entity_type":"faction","name":"Wind Dukes of Aaqa","summary":"Rulers of Aaqa who command the aarakocra to scout the planes and oppose Elemental Evil."},
  {"entity_type":"faction","name":"Elemental Evil","summary":"Malign elemental forces whose temples the aarakocra hunt."}
],
"mentions":[
  {"entity_name":"Elemental Plane of Air","mention_text":"the Howling Gyre surrounds Aaqa in the Elemental Plane of Air"}
],
"relationships":[
  {"from_name":"Aarakocra","to_name":"Aaqa","rel_type":"LOCATED_IN"},
  {"from_name":"Aarakocra","to_name":"Wind Dukes of Aaqa","rel_type":"SERVES"},
  {"from_name":"Aarakocra","to_name":"Elemental Evil","rel_type":"ENEMY_OF"},
  {"from_name":"Aaqa","to_name":"Elemental Plane of Air","rel_type":"LOCATED_IN"}
]}
("gargoyles" is a generic type, so no Gargoyle entity and no edge to it. Canonical singular \
Title Case names throughout.)

Example 2 - pure rules glossary (a rulebook chapter)
Input:
Book: System Reference Document (rulebook)
Section: CASTING A SPELL

When a character casts any spell, the same basic rules are followed. Each spell \
description begins with a block of information, including the spell's name, level, school \
of magic, casting time, range, components, and duration.
Output:
{"entities":[],"mentions":[],"relationships":[]}

Example 3 - adventure module, map-key location heading with hierarchy
Input:
Book: Curse of Strahd (adventure)
Section: Chapter 13: The Abbey of Saint Markovia > L17. Surgery

This blood-stained chamber served as the abbey's surgery. Rusted implements still hang \
above a stone slab.
Output:
{"entities":[
  {"entity_type":"location","name":"Surgery","summary":"A blood-stained former surgery in the Abbey of Saint Markovia, its rusted implements still hanging above a stone slab.","detail":{"location_type":"room"}}
],
"mentions":[
  {"entity_name":"Abbey of Saint Markovia","mention_text":"the surgery is a chamber within the Abbey of Saint Markovia"}
],
"relationships":[
  {"from_name":"Surgery","to_name":"Abbey of Saint Markovia","rel_type":"LOCATED_IN"}
]}
(The map-key prefix "L17." is stripped to name the leaf "Surgery"; the breadcrumb ancestor \
gives the LOCATED_IN parent.)

Example 4 - spellbook stat block, the stat-blocked spell is a valid entity
Input:
Book: Player's Handbook (rulebook)
Section: FIREBALL

Fireball - 3rd-level evocation. Casting Time: 1 action. Range: 150 feet. Components: V, \
S, M (a tiny ball of bat guano and sulfur). Duration: Instantaneous. A bright streak \
flashes to a point you choose, where it blossoms into flame. Each creature in a 20-foot \
sphere makes a Dexterity save, taking 8d6 fire damage on a failure.
Output:
{"entities":[
  {"entity_type":"spell","name":"Fireball","summary":"A 3rd-level evocation that detonates in a 20-foot sphere, dealing 8d6 fire damage on a failed Dexterity save.","detail":{"level":3,"school":"evocation","casting_time":"1 action","range":"150 feet","components":"V, S, M (a tiny ball of bat guano and sulfur)","duration":"Instantaneous","description":"A bright streak blossoms into an explosion of flame in a 20-foot-radius sphere."}}
],
"mentions":[],
"relationships":[]}"""


# v3: v2 verbatim PLUS a relationship type-signature table, a magical-exception
# caveat, and a person-as-item nudge (spec). The validation batch on
# lost-mine-of-phandelver confirmed v2's enrichment and dungeon-room entities are
# WANTED, so v3 keeps everything v2 does (book title, section path, dungeon rooms,
# closed rel enum) and only adds relationship-structure guidance to cut the
# residual wrong_direction / type_mismatch / person-as-item defects. Built by
# concatenation so v2 stays byte-frozen; do NOT edit this text once released, add
# a v4 instead.
_V3_REL_SIGNATURE_ADDENDUM = """

RELATIONSHIP TYPE SIGNATURES (defaults you must follow; a wrong-typed or backwards edge is worse than no edge)
Treat "agent" as an npc, creature, faction, or deity, and "place" as a location. Each rel_type has a default type signature and direction:
- LOCATED_IN: any -> place. CONTAINS: place -> any. PART_OF: place -> place, or faction -> faction.
- NEAR, CONNECTS_TO: place <-> place (symmetric).
- MEMBER_OF, FOUNDED: agent -> faction.
- LEADER_OF: npc / creature / deity -> faction (leader of an ORGANIZATION, NEVER of a place; for "rules a place" use LEADER_OF against the place's ruling faction, or LOCATED_IN).
- SERVES: agent -> npc / faction / deity (NEVER serve a place).
- ALLY_OF, ENEMY_OF: agent <-> agent (symmetric; NEVER point one at a place).
- WORSHIPS: agent -> deity.
- CREATED_BY / CREATES: a thing and its creator (CREATED_BY points thing -> creator; CREATES points creator -> thing).
- CASTS: npc / creature -> spell. OWNS, WIELDS: npc / creature -> item.
- VARIANT_OF: same-type -> same-type. ORIGINATES_FROM: creature / npc / faction / item -> location / faction.
- Kinship (PARENT_OF, CHILD_OF, SIBLING_OF, ANCESTOR_OF, DESCENDANT_OF, SPOUSE_OF): npc / creature / deity <-> npc / creature / deity.
- RELATED_TO: any <-> any (the fallback when nothing above fits).

These signatures and directions are DEFAULTS, not anti-hallucination rules: keep extracting every connection the chunk supports. If the text explicitly describes a magical exception (a location that exists inside an item, a pocket dimension, a demiplane bound to an object), follow the text. Otherwise obey the signatures and get the DIRECTION right: a person is LOCATED_IN a castle, the castle is not LOCATED_IN the person; a servant SERVES a master, never a place.

A NAMED IDENTITY IS AN NPC, NOT AN ITEM
A named identity or alias, even one that names an object (a disguised villain, a masked persona, "the Glass Staff" used as a person's alias), is an npc, not an item. The item is the object that person wields or owns; the person is a separate npc entity. Never type a character as an item just because their alias names a thing."""

_V3_PROMPT_TEXT = _V2_PROMPT_TEXT + _V3_REL_SIGNATURE_ADDENDUM


# v4: generic typed extraction over lore + gameplay + mechanics (spec v4). v3
# verbatim (so v3 stays byte-frozen) PLUS a taxonomy addendum that widens the
# entity vocabulary from the 7 lore types to ~18, introduces the derived category
# and the event/quest temporality, promotes game mechanics (conditions, feats,
# races, classes, class features, actions, rules) to first-class entities, and
# adds the new relationship types. All of v3's lore/enrichment, dungeon-room,
# canonical-naming, and relationship-direction guidance still applies. Built by
# concatenation; do NOT edit this text once released, add a v5 instead.
_V4_TAXONOMY_ADDENDUM = """

GENERIC TYPED EXTRACTION (v4): LORE + GAMEPLAY + MECHANICS
Everything above still holds. This section WIDENS what counts as an entity beyond the seven lore types. Each entity_type below derives a category automatically (you do NOT emit category): lore, gameplay, or mechanics.

- category=lore (unchanged): creature, npc, location, faction, deity, item, spell.
- category=gameplay: event, quest. Each carries a "temporality" field (see below).
- category=mechanics: condition, feat, race, background, class, subclass, class_feature, action, rule.

WHAT COUNTS for the new types (extract when the chunk genuinely describes one; a bare mention still belongs in "mentions"):
- event: a specific happening in the world's history or the adventure's plot (a battle, a cataclysm, a founding, a prophesied doom). Not a generic recurring activity.
- quest: an objective the party can pursue (a main plot goal, a side task, or an adventure hook), typically with a giver, an objective, and a reward.
- condition: a defined game condition or status effect (Poisoned, Grappled, Frightened, Exhaustion).
- feat: a named character feat (Sentinel, Great Weapon Master).
- race: a playable species/ancestry (Elf, Dwarf, Tiefling) and its traits.
- background: a character background (Acolyte, Criminal, Folk Hero).
- class: a character class (Wizard, Fighter, Cleric).
- subclass: a class's subclass/archetype (Circle of the Moon, School of Evocation).
- class_feature: a feature a class or subclass grants at a level (Rage, Sneak Attack, Wild Shape). A class feature is class_feature, NEVER spell, even when it resembles magic.
- action: a defined action, bonus action, or reaction available in play (Dodge, Dash, Opportunity Attack).
- rule: a named rules subsystem or procedure worth capturing as an entity (Concentration, Cover, Exhaustion track). Keep pure prose rules text that names nothing as empty, as before.

TEMPORALITY (event and quest only; set the top-level "temporality" field, one of historical|present|future):
- historical: setting backstory or a past occurrence (an ancient war, a fallen empire).
- present: current to the adventure (an ongoing siege, an active quest).
- future: prophesied or scheduled (a foretold return, a planned ritual).
Do NOT set temporality for any other type.

MECHANICS GUIDANCE
Extract game mechanics wherever they appear: in rulebooks (a class chapter, the conditions appendix) AND when an adventure or guide references them by name. This does NOT reopen the generic-suppression rule: a generic monster or mundane item mentioned in a scene is still furniture (put it in mentions). But a named condition, feat, race, class, subclass, class feature, action, or rule is now a first-class entity, not junk. Reconciling with the v3 "CLASS FEATURES ARE NOT SPELLS" rule: a class feature is now its OWN entity_type class_feature (still never spell).

DETAIL fields for the new types (include only what the text supports; omit the rest):
- event: temporality(historical|present|future), when(str), when_ordering(str), where(str), participants(array), outcome(str)
- quest: temporality(present|future|historical), objective(str), giver(str), reward(str or array), quest_type(main|side|hook)
- condition: effects(str)
- feat: prerequisite(str), benefit(str)
- race: ability_scores(object), traits(object), speed(str), size(str), subraces(array)
- background: proficiencies(str), feature(str), equipment(str)
- class: hit_die(str), primary_ability(str), saves(str), features_by_level(object)
- subclass: parent_class(str), features_by_level(object)
- class_feature: class(str), subclass(str), level(int), description(str)
- action: action_type(action|bonus|reaction), description(str)
- rule: rule_category(combat|exploration|social|magic), description(str)

NEW RELATIONSHIP TYPES (added to the closed set; same direction discipline as above, a wrong-typed edge is worse than no edge):
- OCCURRED_AT: event -> location (where an event happened).
- INVOLVED: event -> agent (npc/creature/faction/deity that took part).
- PRECEDED: event -> event (the from-event happened before the to-event). CAUSED: event -> event (the from-event caused the to-event).
- ADVANCES: event -> quest (an event that moves a quest forward).
- GIVEN_BY: quest -> npc (the quest giver). OBJECTIVE_AT: quest -> location (where the objective is). REWARDS: quest -> item (an item the quest grants).
- SUBCLASS_OF: subclass -> class. FEATURE_OF: class_feature -> class or subclass. REQUIRES: feat -> feat, class, or race (a prerequisite).
- Reuse the existing set where it fits: PART_OF for a sub-event/sub-quest of a larger one, GRANTS for a class/subclass/feat that grants a spell or class feature, RELATED_TO as the fallback."""

_V4_PROMPT_TEXT = _V3_PROMPT_TEXT + _V4_TAXONOMY_ADDENDUM


# v5: prompt-only tune of two mechanics-extraction drifts a two-sample SRD 5.2
# eval surfaced in v4. v4 verbatim (so v4 stays byte-frozen) PLUS a short
# addendum that tightens exactly two definitions: (1) condition was over-eager,
# sweeping in spells, items, senses/traits, and general mechanics; (2)
# class_feature was conflated with monster/NPC innate abilities (135 FEATURE_OF
# edges to a creature/npc were downgraded to RELATED_TO by the validator). No
# schema, REL_SIGNATURES, or migration change: this is prompt text only. Built by
# concatenation; do NOT edit this text once released, add a v6 instead.
_V5_MECHANICS_CLARIFICATION_ADDENDUM = """

MECHANICS CLARIFICATIONS (v5): condition scope and class_feature vs monster abilities
Everything above still holds. These two clarifications tighten what counts as a condition and a class_feature; nothing else changes.

CONDITION SCOPE
A `condition` is a status effect that changes a creature's capabilities (e.g. Prone, Frightened, Grappled, Incapacitated, Poisoned, Exhaustion, Restrained). Do NOT classify a spell (`Dancing Lights`), an item (`Scrolls`), a sense or trait (`Darkvision`), or a general mechanic (`Advantage`) as a condition.

CLASS_FEATURE VS MONSTER ABILITIES
A `class_feature` is a feature a PLAYER CHARACTER gains from a class or subclass (e.g. Sneak Attack, Rage, Second Wind, Wild Shape), and connects `FEATURE_OF` a `class` or `subclass`. A monster's or NPC's innate traits/actions are part of that creature's own detail, NOT a `class_feature` - do not extract monster abilities as `class_feature`."""

_V5_PROMPT_TEXT = _V4_PROMPT_TEXT + _V5_MECHANICS_CLARIFICATION_ADDENDUM


# v6: two extraction defects the first full v5 corpus run surfaced (see
# grimoire/prompt-backlog.md). (1) Treasure/magic-item tables and spell lists were
# exploded into dozens of junk row entities ("+1 Arrow", "10 Gems worth 100 GP")
# and a bare "3rd Level" spell-list heading became a phantom class_feature; the new
# ``table`` type captures a whole table as ONE entity. (2) 2024-format class
# features under "LEVEL N: NAME" headings returned empty (recall loss). v5 verbatim
# (so v5 stays byte-frozen) PLUS an addendum; the schema object is unchanged (it now
# carries the wider entity_type enum via EXTRACT_SCHEMA). Built by concatenation; do
# NOT edit this text once released, add a v7 instead.
_V6_TABLE_AND_RECALL_ADDENDUM = """

TABLES, CLASS FEATURE HEADINGS, AND ANTHOLOGIES (v6)
Everything above still holds. These four clarifications change nothing else.

TABLES ARE ONE ENTITY, NEVER ROW ENTITIES
When a chunk is a table or a list of options (a treasure or magic-item table, a spell list, a class progression or level table, a random-encounter table, a rarity table), emit ONE entity with entity_type "table", named from the table's caption or the section heading (never from a row), and put the structure in "detail": dice (str, e.g. "d100", when it is a rollable table), columns (array of column names), rows (object keyed by the roll range or row index, with the row's text as the value). Do NOT emit the rows as entities: "+1 Arrow" from a treasure table, a spell name from a class spell list, or a level row is NOT an item, spell, or class_feature entity. A row's subject is an entity only when it is separately the stat-blocked subject of its own entry elsewhere. If the table continues from a previous chunk (no caption, rows only), still emit the same-named table entity with the additional rows.
The table's "summary" MUST say what the table is FOR and what its rows contain, so it can be found by meaning, not just by caption: "Random magic items of common and uncommon rarity for treasure hoards at levels 5-10, rolled on a d100", never a restatement of the caption like "A table of magic items." The table entity's summary MUST be genuinely descriptive: say what the table is FOR and what its rows contain, because only name + summary is embedded for search (the rows in detail are never embedded), so a bare caption restatement is invisible to search by meaning. Write "Random magic items of common and uncommon rarity for treasure hoards at levels 5-10, rolled on a d100", NOT "A table of magic items", and never leave the summary as a restatement of the caption.

CLASS FEATURE HEADINGS
In a class or subclass chapter, a heading of the form "LEVEL N: FEATURE NAME" (for example "LEVEL 3: HAND OF HEALING", "LEVEL 7: EVASION") IS that class or subclass feature: extract it as class_feature with detail.level = N. But a BARE level heading with no feature name ("3RD LEVEL", "10th Level") sitting over a list of spells or options is a table or list heading, NOT a class_feature; capture it as a "table".

ADVENTURE ANTHOLOGIES
A book genre of "adventure-anthology" is a collection of standalone adventures; apply the adventure-module rules to each adventure's content.

Keep it tight: these clarifications change nothing else; everything above still holds."""

_V6_PROMPT_TEXT = _V5_PROMPT_TEXT + _V6_TABLE_AND_RECALL_ADDENDUM


PROMPT_VERSIONS: dict[str, PromptVersion] = {
    "v1": PromptVersion(text=_V1_PROMPT_TEXT, schema=None),
    "v2": PromptVersion(text=_V2_PROMPT_TEXT, schema=EXTRACT_SCHEMA),
    "v3": PromptVersion(text=_V3_PROMPT_TEXT, schema=EXTRACT_SCHEMA),
    "v4": PromptVersion(text=_V4_PROMPT_TEXT, schema=EXTRACT_SCHEMA),
    "v5": PromptVersion(text=_V5_PROMPT_TEXT, schema=EXTRACT_SCHEMA),
    "v6": PromptVersion(text=_V6_PROMPT_TEXT, schema=EXTRACT_SCHEMA),
}

# The version the extraction pass writes and reads by default. Env-overridable so
# a candidate (v7) can run on a fresh book without touching code; promotion is
# moving this pointer. Read at import: jobs run as fresh processes, so the env is
# honored per run.
ACTIVE_PROMPT_VERSION = os.environ.get("GRIMOIRE_PROMPT_VERSION", "v6")

# Backward-compatible alias: the active version's system text. The
# self-correction turn and any caller referencing EXTRACTION_PROMPT get the live
# prompt without knowing the registry.
EXTRACTION_PROMPT = PROMPT_VERSIONS[ACTIVE_PROMPT_VERSION].text


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
        prompt_version: str | None = None,
    ):
        # Provider-agnostic key env: prefer GRIMOIRE_EXTRACT_API_KEY (the key for
        # whatever provider GRIMOIRE_EXTRACT_BASE_URL points at, e.g. direct
        # DeepSeek), falling back to OPENROUTER_API_KEY for back-compat. An
        # explicit arg always wins; a keyless in-cluster vLLM passes "" with
        # neither env set, so it stays keyless.
        self.api_key = (
            api_key
            or os.environ.get("GRIMOIRE_EXTRACT_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY", "")
        )
        self.model = model or os.environ.get("GRIMOIRE_EXTRACT_MODEL", DEFAULT_MODEL)
        self.base_url = base_url or os.environ.get(
            "GRIMOIRE_EXTRACT_BASE_URL", OPENROUTER_URL
        )
        # The label is part of the marker key, so it must be a registered
        # version. Fall back to ACTIVE_PROMPT_VERSION on an unknown label rather
        # than KeyError-ing a whole run.
        self.prompt_version = prompt_version or ACTIVE_PROMPT_VERSION
        if self.prompt_version not in PROMPT_VERSIONS:
            logger.warning(
                "grimoire extract: unknown GRIMOIRE_PROMPT_VERSION %r, using %s",
                self.prompt_version,
                ACTIVE_PROMPT_VERSION,
            )
            self.prompt_version = ACTIVE_PROMPT_VERSION
        self._version = PROMPT_VERSIONS[self.prompt_version]

    def _headers(self) -> dict:
        """Bearer auth header when an api_key is set, empty dict otherwise.

        Keyless in-cluster endpoints (e.g. local Qwen via vLLM) do not need
        or accept an Authorization header.
        """
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _is_openrouter(self) -> bool:
        """True when the endpoint is OpenRouter (the default, or any openrouter.ai
        URL). Drives json_schema-vs-guided_json and the reasoning flag: OpenRouter
        honors ``response_format: json_schema`` and the ``reasoning`` control; an
        in-cluster vLLM endpoint uses ``guided_json`` and ignores ``reasoning``."""
        return (not self.base_url) or "openrouter.ai" in self.base_url

    def _is_deepseek(self) -> bool:
        """True when the endpoint is the direct DeepSeek API (api.deepseek.com).

        DeepSeek is a keyed hosted provider like OpenRouter, but its
        reasoning-off control is ``thinking: {type: disabled}`` (not OpenRouter's
        ``reasoning``), it has no provider-routing concept, and it takes
        ``response_format: json_object`` (not the strict json_schema block)."""
        return "api.deepseek.com" in self.base_url

    @staticmethod
    def _provider_routing() -> dict[str, Any] | None:
        """OpenRouter provider preference from ``GRIMOIRE_EXTRACT_PROVIDER``.

        OpenRouter's cheapest V4-Flash route is an fp4-quantized endpoint
        (DeepInfra), which degrades extraction precision and does not preserve
        the prompt cache. Pinning a first-party / native-precision provider (the
        cronworkflow default ``deepseek``; Fireworks is the documented
        alternative) avoids that. A comma list becomes an ordered array;
        ``allow_fallbacks`` is False so OpenRouter never silently drops to fp4.
        Returns None when the env is unset (no provider constraint sent).
        """
        raw = os.environ.get("GRIMOIRE_EXTRACT_PROVIDER", "").strip()
        if not raw:
            return None
        order = [p.strip() for p in raw.split(",") if p.strip()]
        if not order:
            return None
        return {"order": order, "allow_fallbacks": False}

    def _format_kwargs(self) -> dict[str, Any]:
        """Output-format enforcement for the active prompt version (spec #4).

        No schema (v1): keep ``response_format: json_object`` (valid JSON, no
        shape). With a schema (v2): OpenRouter and direct DeepSeek keep their
        hosted provider dialects; an in-cluster endpoint gets both vLLM's
        ``guided_json`` and llama.cpp's strict JSON Schema ``response_format``.
        Both carry the same schema, so the active engine enforces the enum
        constraints and the unsupported field is inert. The self-correction
        retry stays as a belt for providers that silently downgrade the format.
        """
        schema = self._version.schema
        if schema is None:
            return {"response_format": {"type": "json_object"}}
        if self._is_openrouter():
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "grimoire_extraction",
                        "strict": True,
                        "schema": schema,
                    },
                }
            }
        if self._is_deepseek():
            return {"response_format": {"type": "json_object"}}
        return shared.inference.structured_output(schema, name="grimoire_extraction")

    @staticmethod
    def _user_message(
        content: str,
        section_path: str | None,
        image_ref: str | None,
        book_title: str | None = None,
        book_kind: str | None = None,
    ) -> str:
        """Build the user turn (spec #2, the +1 Plate Armor / OCR / map-key /
        genre root-cause fix): the book title + genre, then the section path
        (a hierarchy breadcrumb when available, else the leaf heading), then the
        chunk body, with an image-chunk signal so the model treats a caption as a
        described illustration, not prose. Each line is optional; a chunk with no
        book/section/image context sends the bare body.
        """
        lines: list[str] = []
        if book_title:
            lines.append(
                f"Book: {book_title}" + (f" ({book_kind})" if book_kind else "")
            )
        if section_path:
            lines.append(f"Section: {section_path}")
        if image_ref:
            lines.append(
                "[This chunk is the caption/description of an illustration "
                "from the sourcebook.]"
            )
        if lines:
            return "\n".join(lines) + "\n\n" + content
        return content

    async def extract(
        self,
        chunk_text: str,
        section_path: str | None = None,
        image_ref: str | None = None,
        book_title: str | None = None,
        book_kind: str | None = None,
    ) -> dict:
        """Extract structured JSON from one chunk of text.

        The user turn carries the book title + genre (``book_title`` /
        ``book_kind``), the section path (``section_path``, a hierarchy breadcrumb
        or leaf heading), and an image-chunk signal (``image_ref``) ahead of the
        body, so the model has the heading's canonical spelling to anchor names to
        and the genre to apply the stat-block split (spec #2).

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
            {"role": "system", "content": self._version.text},
            {
                "role": "user",
                "content": self._user_message(
                    chunk_text, section_path, image_ref, book_title, book_kind
                ),
            },
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
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        payload.update(self._format_kwargs())
        # Disable reasoning tokens for the structured-extraction task (spec #5),
        # using the provider's own control. Direct DeepSeek uses
        # ``thinking: {type: disabled}``; OpenRouter uses ``reasoning`` and also
        # accepts a provider-routing block. A vLLM endpoint takes neither.
        if self._is_deepseek():
            payload["thinking"] = {"type": "disabled"}
        elif self._is_openrouter():
            payload["reasoning"] = {"enabled": False}
            provider = self._provider_routing()
            if provider is not None:
                payload["provider"] = provider
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


# Numeric detail fields gated on being anchored in the chunk text (spec Change 3):
# creature ac/hp_avg/cr and spell level. On a chunk with no stat block the model
# sometimes fills these from memory, and on third-party books those are reliably
# wrong. A value that fails the check is dropped (never fails the chunk) and
# counted in the run's ``detail_ungrounded_drops``. Ability scores, speed,
# actions, traits, and prose fields are NOT gated (a post-run verifier covers
# those).
_GROUNDED_NUMERIC_FIELDS: frozenset[str] = frozenset({"ac", "hp_avg", "cr", "level"})

# Non-integral CRs that appear as a fraction in stat blocks. A decimal cr maps to
# both its decimal spelling and this fraction; an integral cr appears as a plain
# int ("Challenge 2"), handled by ``{value:g}``.
_CR_FRACTIONS: dict[float, str] = {0.125: "1/8", 0.25: "1/4", 0.5: "1/2"}


def _cr_text_variants(value: float) -> list[str]:
    """Textual forms a CR value may take in a stat block: the decimal spelling
    ("2", "0.5") plus the canonical fraction for the three fractional CRs."""
    variants = [f"{value:g}"]
    fraction = _CR_FRACTIONS.get(round(float(value), 3))
    if fraction is not None:
        variants.append(fraction)
    return variants


def _grounded_numeric(chunk_text: str, field_name: str, value: Any) -> bool:
    """Whether a numeric detail value is anchored in the chunk text (spec Change 3).

    Case-insensitive. ``ac``/``hp_avg`` match their stat-block label followed by
    the integer; ``cr`` matches Challenge/CR followed by the decimal or fraction
    form; spell ``level`` matches "cantrip" for 0 and the ordinal or "level N"
    form for N >= 1. A field not covered here is treated as grounded.
    """
    flags = re.IGNORECASE
    if field_name == "ac":
        v = re.escape(str(int(value)))
        return bool(
            re.search(rf"\bArmor Class[:\s]*{v}\b", chunk_text, flags)
            or re.search(rf"\bAC[:\s]*{v}\b", chunk_text, flags)
        )
    if field_name == "hp_avg":
        v = re.escape(str(int(value)))
        return bool(
            re.search(rf"\bHit Points[:\s]*{v}\b", chunk_text, flags)
            or re.search(rf"\bHP[:\s]*{v}\b", chunk_text, flags)
        )
    if field_name == "cr":
        alts = "|".join(re.escape(v) for v in _cr_text_variants(float(value)))
        return bool(
            re.search(rf"\bChallenge[:\s]*(?:{alts})\b", chunk_text, flags)
            or re.search(rf"\bCR[:\s]*(?:{alts})\b", chunk_text, flags)
        )
    if field_name == "level":
        n = int(value)
        if n == 0:
            return bool(re.search(r"\bcantrip\b", chunk_text, flags))
        return bool(
            re.search(rf"\b{n}(?:st|nd|rd|th)[-\s]level", chunk_text, flags)
            or re.search(rf"\blevel {n}\b", chunk_text, flags)
        )
    return True


def _coerce_detail_fields(
    detail_model: type,
    entity_name: str,
    detail: dict,
    chunk_text: str,
    drops: dict[str, int],
) -> dict[str, Any]:
    """Coerce a raw detail payload to typed field kwargs (no entity_id).

    Any key in ``detail`` not in the model's known field set is silently
    ignored (defensive against the model inventing extra fields); a known
    field whose value cannot be coerced to the expected type is dropped
    with a warning rather than failing the whole chunk. The four numeric
    stat fields in ``_GROUNDED_NUMERIC_FIELDS`` are additionally dropped (and
    tallied in ``drops``) when their value is not anchored in ``chunk_text``.
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
            coerced_value = expected_type(value)
        except (TypeError, ValueError):
            logger.warning(
                "grimoire extract: dropping uncoercible detail field %s.%s=%r for %r",
                detail_model.__tablename__,
                field_name,
                value,
                entity_name,
            )
            continue
        if field_name in _GROUNDED_NUMERIC_FIELDS and not _grounded_numeric(
            chunk_text, field_name, coerced_value
        ):
            drops[field_name] = drops.get(field_name, 0) + 1
            continue
        coerced[field_name] = coerced_value
    return coerced


def _apply_generic_detail(entity: Entity, item: dict) -> None:
    """Route a new-type entity's detail into the generic JSONB column and lift
    temporality onto the spine (spec v4).

    Used for the gameplay/mechanics types (and any spine-only lore type) that have
    no dedicated detail table; creature/location/npc/spell keep their typed tables
    and never reach here. Enrich, not overwrite, to match ADR 012 rev.: JSONB keys
    are merged with existing keys winning (a fresh dict is reassigned so SQLAlchemy
    flags the column dirty), and temporality (a top-level field or a ``detail`` key)
    fills only when still NULL and only for event/quest.

    The merge is one level deep for nested dict values: a top-level key whose value
    is a dict in both the incoming and stored detail is itself key-merged (existing
    keys winning) rather than replaced wholesale. This is what lets a ``table`` (v6)
    split across chunks accumulate its ``rows`` object, and a class accumulate its
    ``features_by_level`` map, instead of a continuation chunk being dropped. It
    mirrors ``_create_or_enrich_detail``'s JSONB-column enrichment; arrays and
    scalars still take existing-wins wholesale.
    """
    detail = item.get("detail")
    if isinstance(detail, dict) and detail:
        current = entity.detail or {}
        merged = {**detail, **current}
        for key, incoming in detail.items():
            stored = current.get(key)
            if isinstance(incoming, dict) and isinstance(stored, dict):
                merged[key] = {**incoming, **stored}
        entity.detail = merged
    if entity.entity_type in TEMPORAL_TYPES and entity.temporality is None:
        temporality = item.get("temporality")
        if not isinstance(temporality, str) and isinstance(detail, dict):
            temporality = detail.get("temporality")
        if temporality in TEMPORALITY_VALUES:
            entity.temporality = temporality


def _create_or_enrich_detail(
    session: Session,
    detail_model: type,
    entity_id: str,
    entity_name: str,
    detail: dict,
    chunk_text: str,
    drops: dict[str, int],
) -> None:
    """Insert the typed detail row, or enrich an existing one (ADR 012 rev.).

    Enrich, not overwrite: a scalar column is filled only when the stored value
    is still NULL, and a JSONB dict column is key-merged with existing keys
    winning. So a monster whose lore and stat block land in different chunks
    ends up whole, while a later chunk never clobbers an earlier one's value.
    ``chunk_text``/``drops`` flow into the numeric grounding gate in
    ``_coerce_detail_fields``.
    """
    coerced = _coerce_detail_fields(
        detail_model, entity_name, detail, chunk_text, drops
    )
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


# A map-key / catalog prefix on a heading-derived name: an optional 1-2 letter
# area code, digits, an optional trailing letter, then "." or ":" and a space
# (e.g. "L17. ", "P2. ", "17. ", "A1: "). Stripped so "L17. Surgery" dedupes with
# "Surgery" (spec #3 map-key defect). Conservative: it must have a digit and a
# delimiter+space, so real names ("St. Cuthbert") are left alone.
_MAP_KEY_PREFIX_RE = re.compile(r"^[A-Za-z]{0,2}\d+[A-Za-z]?[.:]\s+")
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
# Curly quotes / apostrophes -> straight, so "Uk’otoa" dedupes with "Uk'otoa".
_QUOTE_NORMALIZE = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
}


def _canonicalize_name(name: str) -> str:
    """Belt-and-suspenders canonicalization of an entity name (spec #3).

    The v2 prompt already asks the model to canonicalize; this is the code-side
    net that survives a model slip, since a naming variant permanently splits one
    entity into several (dedup is first-write-wins). It does ONLY the mechanically
    safe normalizations: strip a map-key/catalog prefix, drop a single leading
    article, straighten curly quotes, and collapse whitespace. Case-folding,
    Title-casing, and singular/plural are deliberately left to the prompt: those
    are context-dependent (proper-noun plurals like "Harpers" / "Wind Dukes" must
    not be de-pluralized, "von"/"of" must not be Title-cased) and unsafe to force
    in code.
    """
    for curly, straight in _QUOTE_NORMALIZE.items():
        name = name.replace(curly, straight)
    name = _MAP_KEY_PREFIX_RE.sub("", name)
    name = _LEADING_ARTICLE_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _room_site(chunk: KnowledgeChunk, canonical_name: str) -> str | None:
    """The parent-place ``site`` key for a location that IS this chunk's own keyed
    entry, used to keep same-named rooms in different dungeons/books distinct.

    The breadcrumb (``section_hierarchy``, else ``section_path``) is split on
    " > ". Only when it has >= 2 segments AND its leaf canonicalizes to this
    entity's own name (so the chunk is that location's keyed entry, not a passing
    mention) is the second-to-last segment returned, canonicalized and
    lower-cased, as the site. A prose mention (leaf != name) or a top-level place
    (< 2 segments) returns None, so a bare reference never guesses a site.
    """
    breadcrumb = chunk.section_hierarchy or chunk.section_path
    if not breadcrumb:
        return None
    segments = breadcrumb.split(" > ")
    if len(segments) < 2:
        return None
    if _canonicalize_name(segments[-1]).lower() != canonical_name.lower():
        return None
    return _canonicalize_name(segments[-2]).lower()


def _pick_location_candidate(
    candidates: list[Entity], site: str | None
) -> Entity | None:
    """Choose which same-book, same-name location (if any) to reuse (spec Change 1).

    ``candidates`` are the same-book locations sharing this name, oldest first.
    A room entry (site set) reuses only a candidate whose site matches, else it
    forces a new node. A site-less reference prefers a site-less candidate, else
    reuses the lone candidate when there is exactly one, else stays ambiguous
    (None -> new node) so a bare "Kitchen" never guesses among several rooms.
    """
    if site is not None:
        for candidate in candidates:
            if candidate.site == site:
                return candidate
        return None
    null_site = [c for c in candidates if c.site is None]
    if null_site:
        return null_site[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _get_or_create_entity(
    session: Session,
    chunk: KnowledgeChunk,
    item: dict,
    local_by_name: dict[str, Entity],
    drops: dict[str, int],
) -> tuple[Entity, bool] | None:
    """Resolve or create the Entity spine (+ detail row, if new) for one extracted entity.

    Returns ``(entity, created)`` or None if ``item`` is not a usable entity
    (missing/invalid entity_type or name).

    Dedup is global by ``(entity_type, lower(name))`` for every type except
    ``location`` and ``table``. ``location`` is scoped to ``source_book`` and a
    ``site`` key (the room's parent place; see ``_room_site`` /
    ``_pick_location_candidate``) so same-named rooms across dungeons and books do
    not merge. ``table`` (v6) is scoped to ``source_book`` alone (no site): repeated
    table captions like "Random Encounters" stay distinct across books but a table
    split across chunks in one book reuses the oldest same-named candidate.
    """
    entity_type = item.get("entity_type")
    name = item.get("name")
    if entity_type not in ENTITY_TYPES or not isinstance(name, str) or not name.strip():
        return None
    name = _canonicalize_name(name)
    if not name:
        return None
    name_key = name.lower()

    if entity_type == "location":
        # Book- and site-scoped dedup: a global (location, lower(name)) key would
        # merge every "Kitchen"/"Cellar" room across dungeons and books.
        site = _room_site(chunk, name)
        candidates = list(
            session.execute(
                select(Entity)
                .where(
                    Entity.entity_type == "location",
                    func.lower(Entity.name) == name_key,
                    Entity.source_book == chunk.book_id,
                )
                .order_by(Entity.created_at)
            )
            .scalars()
            .all()
        )
        existing = _pick_location_candidate(candidates, site)
    elif entity_type == "table":
        # Book-scoped dedup WITHOUT site logic (v6): a table caption like "Random
        # Encounters" recurs across books, so scope to source_book and reuse the
        # oldest same-named candidate (a table split across chunks in one book), but
        # never merge tables across books.
        site = None
        existing = (
            session.execute(
                select(Entity)
                .where(
                    Entity.entity_type == "table",
                    func.lower(Entity.name) == name_key,
                    Entity.source_book == chunk.book_id,
                )
                .order_by(Entity.created_at)
            )
            .scalars()
            .first()
        )
    else:
        site = None
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
            site=site,
        )
        session.add(entity)
        session.flush()
        local_by_name[name_key] = entity
        created = True

    # Both paths enrich detail: a new entity's row is created, an existing one
    # is filled where NULL (so a monster split across chunks ends up whole).
    # creature/location/npc/spell use their typed detail tables; every other type
    # (event/quest/mechanics, plus spine-only faction/deity/item) routes into the
    # generic detail JSONB and lifts temporality onto the spine (spec v4).
    detail_model = ENTITY_DETAIL_MODELS.get(entity_type)
    detail = item.get("detail")
    if detail_model is not None:
        if isinstance(detail, dict) and detail:
            _create_or_enrich_detail(
                session, detail_model, entity.id, name, detail, chunk.content, drops
            )
    else:
        _apply_generic_detail(entity, item)

    return entity, created


def _resolve_entity_name(
    session: Session, local_by_name: dict[str, Entity], name: str, book_id: str
) -> Entity | None:
    """Resolve a bare name to an Entity for a mention/relationship endpoint.

    Resolution order: this chunk's freshly-extracted entities (the local map),
    then the oldest same-``book_id`` entity of that name, then the global first
    match. The same-book step keeps an endpoint pointing at its own book's node
    (e.g. a room's book-scoped keyed entry) instead of a same-named node from
    another book. Lookup is not scoped to a single entity_type, matching how
    mentions/relationships reference entities by name alone; an arbitrary match
    is still accepted when several types share a name. The name is canonicalized
    the same way the spine name is, so an endpoint referencing "The Zhentarim"
    resolves to the "Zhentarim" node (spec #3).
    """
    name_key = _canonicalize_name(name).lower()
    if not name_key:
        return None
    if name_key in local_by_name:
        return local_by_name[name_key]
    same_book = (
        session.execute(
            select(Entity)
            .where(func.lower(Entity.name) == name_key, Entity.source_book == book_id)
            .order_by(Entity.created_at)
        )
        .scalars()
        .first()
    )
    if same_book is not None:
        return same_book
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
    session: Session,
    from_entity_id: str,
    to_entity_id: str,
    rel_type: str,
    chunk_id: str,
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
        # First writer wins: the dedup triple already exists, so its chunk_id
        # provenance is left as-is.
        return False
    session.add(
        Relationship(
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            rel_type=rel_type,
            chunk_id=chunk_id,
        )
    )
    return True


def prompt_version_hash(label: str) -> str:
    """SHA-256 of a released version's system text. Used only by the frozen-hash
    test to pin each shipped prompt; NOT part of the marker key (the label is)."""
    return hashlib.sha256(PROMPT_VERSIONS[label].text.encode("utf-8")).hexdigest()


def current_extraction_key() -> tuple[str, str]:
    """The ``(model, prompt_version)`` marker key the extraction pass writes right
    now (env override or DEFAULT_MODEL, active prompt-version label).

    Read paths that report extraction coverage (grimoire.library) count
    ``chunk_extraction`` rows under exactly this key, so a book's "extracted"
    count reflects the live model + prompt version: bumping either resets
    coverage to zero the same way it makes every chunk pending again in
    ``_select_pending_chunks``.
    """
    model = os.environ.get("GRIMOIRE_EXTRACT_MODEL", DEFAULT_MODEL)
    return model, ACTIVE_PROMPT_VERSION


def _select_pending_chunks(
    session: Session, model: str, prompt_version: str, limit: int
) -> list[KnowledgeChunk]:
    """Sync select of up to ``limit`` chunks with no marker yet, oldest first.

    A chunk is pending if there is no ``chunk_extraction`` row for THIS
    exact ``(model, prompt_version)`` key; a different model or prompt version
    makes every chunk pending again. Optional ``GRIMOIRE_EXTRACT_BOOK`` scopes
    selection to one ``book_id`` for staged eval runs. Ordered by
    created_at so ``limit`` is a deterministic FIFO cutoff, not whatever
    order the DB happens to return unprocessed rows in.
    """
    marker_exists = (
        select(ChunkExtraction.chunk_id)
        .where(
            ChunkExtraction.chunk_id == KnowledgeChunk.id,
            ChunkExtraction.model == model,
            ChunkExtraction.prompt_version == prompt_version,
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
    counts: dict[str, Any] = {
        "entities_created": 0,
        "entities_reused": 0,
        "mentions_created": 0,
        "relationships_created": 0,
        # Per-rel_type compliance monitors (keyed by the ORIGINAL rel_type the
        # model emitted): how often the deterministic type-signature validator had
        # to reorder a backwards edge or downgrade an impossible one to RELATED_TO.
        "rel_signature_swaps": {},
        "rel_signature_downgrades": {},
        # Per-field tally of numeric detail values dropped for not being anchored
        # in the chunk text (creature ac/hp_avg/cr, spell level; spec Change 3).
        "detail_ungrounded_drops": {},
        # Edges skipped because both endpoints resolved to the same entity (a
        # self loop, e.g. a validator downgrade produced X RELATED_TO X).
        "rel_self_loops_dropped": 0,
    }
    with session.begin_nested():
        local_by_name: dict[str, Entity] = {}

        for item in extraction["entities"]:
            if not isinstance(item, dict):
                continue
            result = _get_or_create_entity(
                session, chunk, item, local_by_name, counts["detail_ungrounded_drops"]
            )
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
            entity = _resolve_entity_name(session, local_by_name, name, chunk.book_id)
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
            # Safety net (spec #4): map any rel_type outside the closed set to
            # RELATED_TO rather than storing free text. This is the belt for the
            # small strict-schema leak (a provider that silently downgrades
            # json_schema), so the graph keeps a queryable closed vocabulary.
            rel_type = rel_type if rel_type in REL_TYPE_SET else RELATED_TO
            from_entity = _resolve_entity_name(
                session, local_by_name, from_name, chunk.book_id
            )
            to_entity = _resolve_entity_name(
                session, local_by_name, to_name, chunk.book_id
            )
            # Drop edges whose endpoints do not resolve to an emitted/known
            # entity: they would be silently lost at write time anyway, and an
            # unresolvable endpoint is the signature of a generic-type or unnamed
            # target the prompt is told not to connect (spec #4).
            if from_entity is None or to_entity is None:
                logger.debug(
                    "grimoire extract: chunk %s unresolvable relationship %r -> %r",
                    chunk.id,
                    from_name,
                    to_name,
                )
                continue
            # Deterministic type-signature validation (spec #2): now that both
            # endpoints resolve to entities, from_type/to_type are known, so a
            # backwards edge can be swapped and an impossible one downgraded to
            # RELATED_TO (kept, never dropped). Magical exceptions in the text are
            # respected (spatial edges are not reordered except place->agent
            # LOCATED_IN). Runs BEFORE persistence, composing with the non-enum ->
            # RELATED_TO mapping above and the drop-unresolvable logic.
            new_rel_type, swap, action = _apply_rel_signature(
                from_entity.entity_type, to_entity.entity_type, rel_type
            )
            if swap:
                from_entity, to_entity = to_entity, from_entity
            if action == "swap":
                counts["rel_signature_swaps"][rel_type] = (
                    counts["rel_signature_swaps"].get(rel_type, 0) + 1
                )
            elif action == "downgrade":
                counts["rel_signature_downgrades"][rel_type] = (
                    counts["rel_signature_downgrades"].get(rel_type, 0) + 1
                )
            rel_type = new_rel_type
            # Self-loop guard: after resolution and any validator swap, both
            # endpoints can be the same entity (two name variants resolving to one
            # node, sometimes after a downgrade to RELATED_TO). An X -> X edge is
            # never meaningful, so drop it and count it.
            if from_entity.id == to_entity.id:
                counts["rel_self_loops_dropped"] += 1
                continue
            if _insert_relationship(
                session, from_entity.id, to_entity.id, rel_type, chunk.id
            ):
                counts["relationships_created"] += 1

    return counts


def _commit(session: Session) -> None:
    session.commit()


async def _embed_new_entities(
    session: Session,
    embed_client: _Embedder,
    newly_created: list[tuple[Entity, str]],
) -> int:
    """Embed (name + summary) the entities created in one group, in batches.

    ``upsert_embedding_batch`` commits internally, so calling this while the
    group's entities/mentions/markers are still pending in the session
    persists them together with their embeddings in one transaction -- an
    entity and its vector are never split across a crash.
    """
    embedded = 0
    for start in range(0, len(newly_created), EMBED_BATCH_SIZE):
        batch = newly_created[start : start + EMBED_BATCH_SIZE]
        texts = [f"{entity.name}: {summary_text}" for entity, summary_text in batch]
        vectors = await embed_client.embed_batch(texts)
        entities_only = [entity for entity, _ in batch]
        embedded += upsert_embedding_batch(
            session, embed_client.model, "entity", entities_only, vectors
        )
    return embedded


def _section_context(chunk: KnowledgeChunk, kind: str | None) -> str | None:
    """Section breadcrumb to feed the extractor for one chunk (spec Change 2).

    Non-bestiary books get the full ancestry breadcrumb (``section_hierarchy``,
    else the leaf ``section_path``), the same context the model was tuned on. But
    the backfilled bestiary hierarchies mis-nest sibling entries under one heading
    (a large share of a bestiary's chunks falsely nest under a neighbor), so the
    full breadcrumb would feed the model false containment. For a bestiary we pass
    leaf-only context instead: the 2-level ``section_path`` when set, else the last
    " > " segment of the hierarchy.
    """
    if kind == "bestiary":
        if chunk.section_path:
            return chunk.section_path
        if chunk.section_hierarchy:
            return chunk.section_hierarchy.split(" > ")[-1]
        return None
    return chunk.section_hierarchy or chunk.section_path


def _load_book_titles(session: Session) -> dict[str, str]:
    """Load the ``{book_id: display_name}`` map once per run (spec #2 book
    context). Kept a sync helper (not called with per-chunk DB round-trips inside
    the async handler) so ``extract_chunks`` fetches titles a single time and
    threads them into each extract call."""
    rows = session.execute(select(Book.id, Book.display_name)).all()
    return {book_id: display_name for book_id, display_name in rows}


async def extract_chunks(
    session: Session,
    or_client: OpenRouterClient,
    embed_client: _Embedder,
    limit: int = DEFAULT_LIMIT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict:
    """Extract entities/mentions/relationships from up to ``limit`` unprocessed chunks.

    A chunk is "unprocessed" if it has no ``chunk_extraction`` marker row
    for this ``(model, prompt_version)`` key yet (see module docstring).

    Chunks are processed in groups of ``concurrency``. Within a group the
    GPU-bound ``or_client.extract`` calls are issued **concurrently**
    (``asyncio.gather``) so the vLLM server continuous-batches them instead
    of running at batch size 1; the network fan-out is the only concurrent
    step. All Session I/O then runs **serially** over the group's results
    (the sync helpers here are not safe to share across tasks, and entity
    name-dedup reads-then-writes), then the group is committed. This
    per-group commit is incremental durability: a job killed at its deadline
    loses at most the in-flight group, not the whole run (every completed
    group already committed and is skipped on the next run). Each group's new
    entities are embedded before its commit so entity + vector persist
    together.

    Returns
    ``{"chunks_processed", "chunks_failed", "entities_created",
    "entities_reused", "mentions_created", "relationships_created",
    "entities_embedded"}``.
    """
    model = or_client.model
    prompt_version = getattr(or_client, "prompt_version", ACTIVE_PROMPT_VERSION)
    chunks = _select_pending_chunks(session, model, prompt_version, limit)
    # Book title map, fetched once (not per chunk) so the async gather below does
    # no DB round-trip per extract call.
    book_titles = _load_book_titles(session)
    group_size = max(1, concurrency)

    summary: dict[str, Any] = {
        "chunks_processed": 0,
        "chunks_failed": 0,
        "entities_created": 0,
        "entities_reused": 0,
        "mentions_created": 0,
        "relationships_created": 0,
        "entities_embedded": 0,
        # Per-rel_type type-signature validator monitors (dict rel_type -> count),
        # merged across chunks below.
        "rel_signature_swaps": {},
        "rel_signature_downgrades": {},
        # Per-field ungrounded numeric-detail drops (dict field -> count) and the
        # self-loop edge drop count, merged across chunks below.
        "detail_ungrounded_drops": {},
        "rel_self_loops_dropped": 0,
    }

    for group_start in range(0, len(chunks), group_size):
        group = chunks[group_start : group_start + group_size]
        # Concurrent network fan-out: overlap the extract calls so vLLM
        # batches them. No Session is touched here. return_exceptions keeps
        # one chunk's failure from cancelling its siblings' in-flight calls.
        extractions = await asyncio.gather(
            *(
                or_client.extract(
                    chunk.content,
                    # Full hierarchy breadcrumb by default (leaf-only for
                    # bestiaries, whose backfilled hierarchies mis-nest); falls
                    # back to the leaf heading when a chunk predates the backfill.
                    section_path=_section_context(chunk, book_kind(chunk.book_id)),
                    image_ref=chunk.image_ref,
                    book_title=book_titles.get(chunk.book_id, chunk.book_id),
                    book_kind=book_kind(chunk.book_id),
                )
                for chunk in group
            ),
            return_exceptions=True,
        )

        # (entity, embed_text) for entities created in THIS group; captured
        # here rather than read back from Entity (which has no summary
        # column) since the summary only exists transiently in the payload.
        newly_created: list[tuple[Entity, str]] = []

        for chunk, extraction in zip(group, extractions):
            if isinstance(extraction, Exception):
                if isinstance(extraction, (OpenRouterError, ValueError)):
                    logger.warning(
                        "grimoire extract: chunk %s failed extraction: %s",
                        chunk.id,
                        extraction,
                    )
                    summary["chunks_failed"] += 1
                    continue
                # Unexpected error (not the extract client's own failure
                # modes): surface it rather than silently marking the chunk
                # failed. Prior groups are already committed.
                raise extraction

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
                if isinstance(value, dict):
                    # Per-rel_type monitor dicts: merge, summing per rel_type.
                    dest = summary[key]
                    for rel, n in value.items():
                        dest[rel] = dest.get(rel, 0) + n
                else:
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
                        prompt_version=prompt_version,
                        status=status,
                    )
                )

        # Embed this group's new entities (commits entities + vectors
        # together), then a final commit ensures the group's markers/mentions
        # are durable even when it created no new entities.
        summary["entities_embedded"] += await _embed_new_entities(
            session, embed_client, newly_created
        )
        _commit(session)

    logger.info("grimoire extract: %s", summary)
    return summary
