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

The shared `grimoire` schema uses a typed entity spine rather than the
standalone prototype's Firestore and polymorphic JSON model:

- `entity` stores shared identity, provenance, visibility, and hierarchy.
- `entity_creature`, `entity_spell`, `entity_location`, and `entity_npc` hold
  type-specific queryable fields.
- `knowledge_chunk`, `chunk_entity_mention`, `chunk_extraction`, `relationship`,
  and `embedding` provide corpus, graph, extraction, and retrieval state.
- `book` and `adventure` organize source material.
- `campaign` is the trusted shared registry. Its canonical `schema_name` routes
  requests to `grimoire_campaign_<campaign UUID without dashes>`.

Each campaign schema owns `player_character`, `game_session`,
`session_transcript`, and `knowledge_grant`, plus homebrew entity/detail,
relationship, mention, and embedding backing tables. Campaign read views expose
the shared corpus together with only that schema's homebrew overlay. Character
metadata belongs to the campaign working set because it changes during play;
only registry metadata stays shared.

Routing uses SQLAlchemy's per-session `schema_translate_map`, never a
connection `search_path`. The schema name comes only from a registry row and is
checked against the campaign UUID before use. The registry checkout is released
after that immutable metadata is captured, before the routed session checks out
the same pool. Each routed transaction records its validated campaign against
its backend PID and transaction ID, then assumes the
`grimoire_campaign_access` role with `SET LOCAL ROLE`. Row security and view
predicates bind that role to the recorded schema, so commits and pooled
connection reuse cannot carry one campaign's route or authority into another
request. The trusted application role retains provisioning and corpus ingestion
ownership; `PUBLIC` has neither schema creation nor table privileges.

Queryable values use typed columns. Irregular display-only structures may use
JSON. Embeddings share one pgvector-backed retrieval surface.

## Visibility and public access

Private DM routes can read the complete shared corpus and their campaign's
homebrew. Player-scoped reads join the campaign-local grant table to the shared
corpus/homebrew read view, centralize the `is_global OR granted-to-player` rule,
and apply the grant scope when projecting details. Campaign roles receive
`SELECT` only on every read view, including the otherwise-updatable direct
corpus views, DML only on their own campaign backing tables, and no access to
shared tables or another campaign schema. Public corpus routes are read-only.
Full text and page images fail closed unless the book is explicitly classified
as open-licensed; copyrighted books expose only derived entities, graph
structure, and bounded snippets.

The separate `grimoire_chat` schema remains anonymous public corpus chat under
ADR security/005. It has no campaign identity, so its sessions and opt-in shared
snapshots are not campaign transcripts. Campaign play transcripts live in each
campaign's `session_transcript` table.

## Ingestion

Batch commands in `app/jobs_main.py` invoke the domain jobs:

- `grimoire-load-chunks` validates externally produced chunk manifests and
  loads books, adventures, chunks, and embeddings.
- `grimoire-extract-entities` produces typed entities, mentions, and graph
  relationships.
- `grimoire-backfill-hierarchy` repairs or derives entity hierarchy data.

The jobs are discrete read, compute, and write stages with recorded provenance.
Bad inputs fail or dead-letter without partially publishing a book.

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
