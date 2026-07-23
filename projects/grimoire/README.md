# Grimoire

Grimoire is a Postgres-first D&D campaign manager. It ingests sourcebook PDFs,
chunks them structurally, extracts entities (creatures, spells, locations,
NPCs) with an LLM, and serves a per-campaign reader and knowledge browser at
`/app/grimoire` on the monolith. A grant system lets a DM control what each
player character knows about a given entity, from full detail down to
name-only recognition.

The design decisions live in [ADR 011](../../docs/decisions/services/011-grimoire-hot-tier-schema.md)
(schema) and [ADR 012](../../docs/decisions/services/012-grimoire-postgres-first-loom-shaped.md)
(why Postgres first, and how the schema stays compatible with a future Loom
migration), which also covers the concrete v1 build.

## What lives here vs. in the monolith

**This directory (`projects/grimoire/`) is a March-era standalone app**
(Go `api` and `ws-gateway` services, a Firestore backend, a React frontend)
that predates the current design. It is explicitly out of scope for v1 and
slated for separate decommission; nothing here is deployed as part of the
current product. The only file worth reading here is
[`loom-mapping.md`](loom-mapping.md), which fixes the long-term Loom
(Iceberg) target architecture that the current Postgres schema is shaped to
migrate into cleanly.

**The live product is a monolith domain module**, `projects/monolith/grimoire/`
(private tier: registered via `register()` in the monolith's main app, plus a
narrower `register_public()` surface for the public read-only tier), with its
UI as SvelteKit routes under `projects/monolith/frontend/src/routes/private/app/grimoire/`
and `.../routes/public/app/grimoire/`. It has its own Postgres schema, its own
jobs, and its own tests; it does not import anything from this directory.

## Architecture

```
external chunker --> s3://grimoire/chunks/<book>.ndjson --> grimoire_load_chunks job
                                                                    |
                                                                    v
                                                    knowledge_chunk + embedding (pgvector)
                                                                    |
                                                                    v
                                                    grimoire_extract_entities job (LLM)
                                                                    |
                                                                    v
                                    entity (+ typed detail tables) / mentions / relationships
                                                                    |
                                                                    v
                                        /app/grimoire reader + entity browser + search
```

### Ingest pipeline

A separate service chunks sourcebook PDFs and drops per-book NDJSON manifests
at `s3://grimoire/chunks/<book_id>.ndjson`. `projects/monolith/grimoire/marker.py`
converts Marker (Datalab) PDF-extraction JSON into that same NDJSON shape by
grouping blocks into contiguous runs that share a section heading, so a
monster's lore, stat block, and actions land in one chunk even though the
source PDF renders them under multiple same-named sub-headers.

Two daily jobs (`projects/monolith/grimoire/jobs.py`) run off-pod as Argo
CronWorkflows rather than in the monolith pod itself:

- **`grimoire-load-chunks`**: lists the manifests, upserts `knowledge_chunk`
  rows keyed on `(book_id, chunk_ref)`, and embeds new or changed content via
  the shared embedding client. Idempotent, so a re-run over an unchanged
  corpus is cheap.
- **`grimoire-extract-entities`**: for chunks with no extraction yet, calls a
  frontier model (OpenRouter, or the in-cluster Qwen vLLM endpoint) for
  structured entity/mention/relationship extraction, and embeds newly created
  entities. Bounded per run and flagged `heavy` so the job dispatcher never
  co-schedules it with another memory-heavy job. Extraction costs money per
  call, so it is scheduled suspended in `values.yaml` (manual trigger only)
  rather than running automatically every day.

### Reader and visibility

Chunks and entities are corpus-global; what a given player character can see
of an entity is governed by a `knowledge_grant` row scoping visibility to
`full`, `partial`, or `name_only`. One helper
(`projects/monolith/grimoire/visibility.py`) implements that predicate and
projection, and every read path (entity lookup, search, the chunk reader's
"entities on this page" chips) routes through it, so the grant logic exists
in exactly one place. The public tier (`public.py`) skips grants entirely and
serves only the `is_global` corpus, since there is no campaign or viewer to
grant against.

The `/app/grimoire` UI is a campaign picker, a viewpoint switcher (DM or a
specific player character), a book/section/chunk reader, an entity browser
with scope-projected detail cards, and a DM-only grant editor.

## Testing

Sibling `*_test.py` files per module in `projects/monolith/grimoire/`, using
SQLite fixtures (`create_all`) with a stubbed embedding client for the
pgvector-dependent search path. As with the rest of the monolith, there is no
local test loop: changes are pushed and verified on BuildBuddy CI.
