# Grimoire architecture

Grimoire is a domain of the Python Monolith. The former standalone Go API,
React frontend, WebSocket gateway, Redis service, Helm chart, and GCP bootstrap
have been retired. Their implementation remains available in git history.

## Runtime shape

- `grimoire/module.py` composes the domain into the private and public Monolith
  profiles.
- Private routes live under `/api/grimoire` in `router.py`.
- Read-only public routes use the same prefix through `router_public.py`.
- The Svelte frontend lives in `projects/monolith/frontend/src/lib/grimoire`
  and `projects/monolith/frontend/src/routes/public/app/grimoire`.
- Persistent state lives in the Monolith Postgres cluster. There is no separate
  Grimoire deployment or datastore.

## Data model

The `grimoire` schema uses a typed entity spine rather than the standalone
prototype's Firestore and polymorphic JSON model:

- `entity` stores shared identity, provenance, visibility, and hierarchy.
- `entity_creature`, `entity_spell`, `entity_location`, and `entity_npc` hold
  type-specific queryable fields.
- `knowledge_chunk`, `chunk_entity_mention`, `chunk_extraction`, `relationship`,
  and `embedding` provide corpus, graph, extraction, and retrieval state.
- `alias_candidate` records review evidence, state hashes, explicit approvals,
  and completed alias-merge provenance.
- `book` and `adventure` organize source material.
- `campaign`, `player_character`, `game_session`, and `knowledge_grant` hold
  mutable play state and per-player knowledge visibility.

Queryable values use typed columns. Irregular display-only structures may use
JSON. Embeddings share one pgvector-backed retrieval surface.

## Visibility and public access

Private DM routes can read the complete corpus. Player-scoped reads centralize
the `is_global OR granted-to-player` rule and apply the grant scope when
projecting details. Public corpus routes are read-only. Full text and page
images fail closed unless the book is explicitly classified as open-licensed;
copyrighted books expose only derived entities, graph structure, and bounded
snippets.

## Ingestion

Batch commands in `app/jobs_main.py` invoke the domain jobs:

- `grimoire-load-chunks` validates externally produced chunk manifests and
  loads books, adventures, chunks, and embeddings.
- `grimoire-extract-entities` produces typed entities, mentions, and graph
  relationships.
- `grimoire-backfill-hierarchy` repairs or derives entity hierarchy data.

The jobs are discrete read, compute, and write stages with recorded provenance.
Bad inputs fail or dead-letter without partially publishing a book.

## Alias review and merge

The private API exposes the report-first alias pass from ADR services/014:

1. `POST /api/grimoire/alias-candidates/scan` refreshes candidates from
   same-type, same-book short/full-name pairs with co-mention evidence.
2. `GET /api/grimoire/alias-candidates?status=pending` returns the durable review
   queue, including bounded source snippets and the exact state hash.
3. After inspecting that evidence, a verified standing human in the `operators`
   group may call `POST /api/grimoire/alias-candidates/{id}/approve` with
   `survivor_entity_id` and the report's `expected_state_hash`. Reviewer
   attribution comes from the verified principal, never request data.
4. `POST /api/grimoire/alias-candidates/{id}/execute` revalidates the approval,
   rewrites the graph in one transaction, and refreshes the survivor embedding
   when its persisted mention-summary input changes.

The same authorized human may persist a version-bound rejection through
`POST /api/grimoire/alias-candidates/{id}/reject`. Scans refresh its evidence but
never silently reopen it or make it executable. Returning a rejected or stale
candidate to review requires a deliberate, version-bound
`POST /api/grimoire/alias-candidates/{id}/reopen` call. All alias report,
decision, scan, and execution routes require the same standing-human operator
authorization.

Scanning never approves or merges a pair. Approval records the reviewer, time,
survivor, and reviewed state hash. Any later entity, detail, mention, type, book,
site, temporality, or evidence change makes that approval stale and requires
another review.
Execution is replay-safe: a completed candidate returns its existing merged
status, while embedding or database failures leave an approved pair retryable.
The endpoints are registered only on the private Grimoire router.

## Loom compatibility

Postgres is the current source of truth and serving tier. The schema remains
compatible with a future Loom/Iceberg durable tier:

- typed entity tables map to typed object datasets;
- `relationship` maps to link definitions;
- `knowledge_grant` represents the materialized per-player slice;
- embeddings retain model and source lineage;
- session-scoped mutable rows form a potential check-in delta.

Loom checkout and check-in are deferred. They are not part of the active
runtime, and no current code should imply otherwise.

## Deliberately absent

The retired prototype's Cloud Run services, Firestore, Redis fan-out,
standalone authentication, Gemini Live voice pipeline, and separate React UI
are not supported architecture. New Grimoire work belongs in the Monolith
domain, its Svelte frontend, or its batch jobs.
