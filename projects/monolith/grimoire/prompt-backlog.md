# Grimoire extraction prompt backlog

Known gaps in the ACTIVE extraction prompt, recorded so future prompt versions
start from evidence instead of rediscovery. Each entry says what was observed,
why it happens, and the fix shape. Prompt versions are frozen by hash: none of
this edits a released version; each item is a candidate for the NEXT version.

Provenance: the 2026-07-06 Fable final eval (280-chunk three-cell sample plus
live spot-checks of the first full v5 corpus run). Re-run economics: markers key
on `(chunk_id, model, prompt_version)`, so a new version re-extracts only what
you point it at (`GRIMOIRE_EXTRACT_BOOK`); the full corpus costs ~$8, the seven
rulebooks alone under $3.

## v6 candidate: class_feature recall on 2024-format class chapters

**Observed (v5, live run):** named PC class features in players-handbook-2024
returned empty instead of a `class_feature` entity. Spot-check of empty-marked
PHB class-chapter chunks found `LEVEL 3: HAND OF HEALING` and
`LEVEL 7: EVASION` (full mechanics text, clearly named) marked
`status='empty'`. Recall loss only: nothing false is stored, the feature node
just does not exist.

**Why:** two prompt-layer causes compound. The v2 base still says a rulebook
class chapter is rules text ("return empty") and "if it is not clearly a named
entity in its own right, omit it entirely", which v4's mechanics promotion
overrides only by assertion; and the 2024 books use `LEVEL N: FEATURE NAME`
headings that match none of the prompt's examples (Rage, Sneak Attack are named
bare). The model resolves the tension conservatively and omits.

**Fix shape:** one clarifying line in the mechanics section of the next
version, e.g. "In a class chapter, a heading of the form 'LEVEL N: NAME' IS
that class or subclass feature: extract it as class_feature with level N."
Then re-run just the rulebooks under the new version and compare per-class
`class_feature` counts against the published rosters (roughly a dozen features
per PHB class).

**Gate before tuning:** the post-run audit counts `class_feature` yield per
class and samples empty class-chapter chunks; tune only if recall is materially
low so the version churn stays evidence-driven.

## Design gap: DM advice is invisible to the graph

**Observed:** DMG-2024 advice prose (for example "Getting Players Invested",
pacing and conflict-building guidance, the DM's Toolbox chapters) returns empty
by design: it names no entity, so the current taxonomy has nowhere to put it.
Joe's call (2026-07-06): this content is exactly what you want surfaced while
RUNNING a campaign, so treating it as pure suppression is a miss, not a win.

**Options considered (no decision yet; needs its own design pass):**

1. **Do nothing at the entity layer.** Advice chunks are already embedded and
   retrievable by chunk search; the gap is only that the GRAPH cannot point at
   them. Cheapest, but advice stays un-linkable from entities, sessions, or
   prep flows.
2. **New extracted type (e.g. `guidance`)** in the mechanics category: name =
   the advice heading, summary = the actionable gist, detail JSONB for
   applies-to (encounter design, pacing, treasure, social play). Needs a
   migration (entity_type CHECK), a prompt addendum, and dedup thought: advice
   titles repeat across books ("Random Encounters" appears in 10), so it must
   at minimum be book-scoped like locations now are.
3. **Chunk-level topic tagging instead of entities:** classify advice chunks
   into a small topic taxonomy on the chunk row and expose them as a "DM
   guidance" search facet. No graph nodes, no name-dedup problem, and the
   consumer (live-play prep) mostly wants retrieval anyway.

Leaning 3 for the consumer fit and because it sidesteps generic-title dedup,
but this should be decided alongside the live-play prep UX, not inside a prompt
bump.

## Smaller carried items

- **`adventure-anthology` book kind is un-evaluated in the prompt.** The genre
  guidance names "adventure module or setting guide" but not the anthology
  kind added for the adventure layer; the model infers from the substring. If
  anthology extractions look off in audits, add the kind to the genre section
  explicitly.
- **RELATED_TO share.** ~20% of stored edges in the final eval were
  RELATED_TO (safety-net downgrades plus the model's own fallback). Mostly
  harmless mush; if it grows, consider prompting harder for typed edges or
  dropping model-emitted bare RELATED_TO between entities that share no
  mention chunk.
- **Bestiary breadcrumbs are quarantined, not fixed.** Extraction passes
  leaf-only context for `bestiary` books because the backfilled
  `section_hierarchy` mis-nests sibling entries (41% of tome-of-beasts-3
  chunks under one heading). The root fix is marker.py nesting + a re-backfill;
  until then any new consumer of bestiary hierarchy inherits the same poison.
