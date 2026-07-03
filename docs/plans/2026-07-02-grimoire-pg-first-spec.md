# Grimoire v1 spec: Postgres-first, Loom-shaped

Companion to [ADR 012](../decisions/services/012-grimoire-postgres-first-loom-shaped.md) (decision + rationale) and [ADR 011](../decisions/services/011-grimoire-hot-tier-schema.md) (schema rationale). This spec is the concrete v1 build: what tables, routes, jobs, and UI ship, and what is explicitly out of scope.

## 1. Scope

**In:** monolith domain module `grimoire` (private tier), Loom-shaped schema in monolith-pg, S3 chunk ingest + embedding, OpenRouter entity extraction, campaign/PC/grant CRUD, grant-filtered lookup + vector search, a minimal `/app/grimoire` SvelteKit page demoing player-vs-DM visibility.

**Out (deferred):** live session state machine (HP, dice, feed, transcripts), realtime fan-out, voice, Loom checkout/check-in, entity resolution across books, encounter generation, the March-era standalone `projects/grimoire/{api,ws-gateway,frontend}` (untouched; decommission separately).

## 2. Module layout

`projects/monolith/grimoire/` following ADR 010:

```
grimoire/
├── __init__.py        # register(app); no register_public (private tier only)
├── models.py          # SQLModel tables (below)
├── router.py          # /api/grimoire routes
├── visibility.py      # the one read-predicate + scope-projection helper
├── search.py          # grant-filtered kNN over embedding
├── ingest.py          # chunk loader (S3 -> knowledge_chunk + embedding)
├── extract.py         # OpenRouter chunk -> entities/mentions/relationships
├── jobs.py            # offloaded job handlers wrapping ingest/extract
└── *_test.py          # sibling tests (hand-add py_test targets in BUILD)
```

Registration: `grimoire.register(app)` in `app/main.py` only. Routes must NOT appear in the public tier (CI's `main_public_routes_test.py` enforces).

## 3. Schema

One Postgres schema `grimoire` (corpus + campaign tables together in v1; the ADR 011 two-schema split is a checkout-time concern that does not exist yet). All tables SQLModel, migration via `chart/migrations/` (next free ID at implementation time). SQLite test fixtures use `create_all`; mirror CHECK constraints in `__table_args__`.

### 3.1 Corpus (future Loom `global` datasets)

- **`entity`** (spine): `id` (uuid pk), `entity_type` (text, CHECK in creature|spell|location|npc|faction|deity|item), `name` (text, indexed), `source_type` (CHECK extracted|homebrew), `is_global` (bool, default true), `source_book` (text, nullable), `created_in_session` (uuid, nullable; check-in seam), `created_at`.
- **Typed detail tables** (CTI, pk = fk to spine): v1 ships four, adding more is mechanical:
  - `entity_creature`: `size`, `creature_type`, `ac` (int), `hp_avg` (int), `cr` (numeric), `speed` (jsonb), `ability_scores` (jsonb), `actions` (jsonb), `traits` (jsonb).
  - `entity_spell`: `level` (int), `school`, `casting_time`, `range`, `components`, `duration`, `classes` (jsonb), `description` (text).
  - `entity_location`: `location_type`, `region`, `description` (text).
  - `entity_npc`: `race`, `occupation`, `disposition`, `description` (text).
  - Faction/deity/item entities may exist as spine-only rows (extraction can emit them; detail tables follow later).
- **`knowledge_chunk`**: `id` (uuid pk), `book_id` (text), `chunk_ref` (text; source id from the external chunker), `content` (text), `section_path` (text, nullable), `created_at`. Unique on `(book_id, chunk_ref)` for idempotent reloads.
- **`chunk_entity_mention`**: `chunk_id` fk, `entity_id` fk, `mention_text`, unique pair.
- **`relationship`**: `id`, `from_entity_id`, `to_entity_id`, `rel_type` (text), `properties` (jsonb), unique `(from, to, rel_type)`.
- **`embedding`** (one ANN surface): `id`, `embeddable_kind` (CHECK entity|chunk|transcript), `embeddable_id` (uuid), `model` (text), `dim` (int), `vector` (pgvector `Vector(1024)`), unique `(embeddable_kind, embeddable_id, model)`. Cosine distance; ivfflat/hnsw index deferred until row counts justify it (seq scan is fine at v1 scale).

### 3.2 Campaign (future `facts_<player>` slices + operational)

- **`campaign`**: `id`, `name`, `dm_name`, `created_at`.
- **`player_character`**: `id`, `campaign_id` fk, `player_name`, `character_name`, `class_name`, `level` (int), `sheet` (jsonb).
- **`game_session`**: `id`, `campaign_id` fk, `status` (CHECK active|paused|ended), `started_at`, `ended_at`. Partial unique index enforcing at most one non-ended session per campaign (the single-active-session invariant; mirror as an app-level check for SQLite fixtures).
- **`knowledge_grant`**: `id`, `campaign_id` fk, `entity_id` fk, `player_character_id` fk, `grant_scope` (CHECK full|partial|name_only), `revealed_details` (jsonb, nullable), `granted_in_session` (uuid, nullable), `created_at`, unique `(entity_id, player_character_id)`.

### 3.3 Visibility semantics (the load-bearing part)

One helper (`visibility.py`) implements ADR 011's predicate and projection, used by every read path:

- Player view: `entity.is_global OR EXISTS grant for (entity, me in this campaign)`.
- Projection by scope: `full` = spine + typed detail; `partial` = spine + `revealed_details` only; `name_only` = recognition only. `name_only` rows are dropped from direct lookup and vector-search results, kept only in relationship context.
- DM view: no predicate, everything, grants visible as annotations.
- Vector search filters candidate ids through the same predicate before returning hits (over-fetch kNN, filter, trim; correct and simple at this scale).

## 4. Ingest pipeline

### 4.1 Bucket + input contract

New SeaweedFS bucket `grimoire`. The external chunking service (or a one-shot gdrive sync) drops per-book NDJSON manifests at `s3://grimoire/chunks/<book_id>.ndjson`, one chunk per line:

```json
{
  "chunk_ref": "phb-c3-014",
  "content": "…chunk text…",
  "section_path": "Chapter 3 > Classes > Wizard",
  "meta": {}
}
```

`book_id` comes from the filename. The loader validates lines against this shape (versioned; unknown fields ignored); invalid lines are logged and skipped with a count, never fail the batch.

### 4.2 Jobs (offloaded, Transform-shaped)

Two batch jobs registered via `on_startup_jobs`, manually triggerable through the existing scheduler/agent tooling, each idempotent:

1. **`grimoire_load_chunks`**: list `s3://grimoire/chunks/`, upsert `knowledge_chunk` by `(book_id, chunk_ref)`, embed new/changed content via `shared/embedding.py` (`EmbeddingClient`, batch API), upsert `embedding` rows with `model`/`dim` recorded.
2. **`grimoire_extract_entities`**: for chunks without mentions, call OpenRouter (structured JSON output: entities with `entity_type` + typed fields, mentions, relationships), upsert entities by `(entity_type, name)` name-dedup, insert mentions + relationships, embed new entities (name + summary text). Model configurable via values env (default a current frontier model); `OPENROUTER_API_KEY` via `OnePasswordItem`. Batch size + max-chunks-per-run bounded via env so a run fits the job deadline.

Provenance: entities carry `source_book`; chunks carry `chunk_ref`; embeddings carry `model`. Enough to re-derive or re-embed (the Loom Transform lineage contract, cheap form).

### 4.3 Secrets and config

`OnePasswordItem` for OpenRouter key; S3 credentials reuse the monolith's existing SeaweedFS access pattern. New env in `values.yaml`: `OPENROUTER_API_KEY` (secretRef), `GRIMOIRE_S3_BUCKET`, `GRIMOIRE_EXTRACT_MODEL`. Chart bump required (Chart.yaml + application.yaml targetRevision).

## 5. API surface (`/api/grimoire`, private tier)

CRUD kept to what the demo needs:

- `POST/GET /campaigns`, `GET /campaigns/{id}`
- `POST/GET /campaigns/{id}/characters`
- `POST/GET/PATCH /campaigns/{id}/grants` (DM grant flips: create/update scope)
- `GET /campaigns/{id}/entities?as={pc_id|dm}&type=&q=` (grant-filtered list/lookup; name search)
- `GET /campaigns/{id}/entities/{entity_id}?as=` (scope-projected detail; 404-equivalent for non-visible and `name_only`)
- `GET /campaigns/{id}/search?as=&q=` (kNN over embedding, grant-filtered, mixed entity/chunk hits with scores)
- `GET /entities/{id}/relationships?as=` (1-hop edges, `name_only` neighbors appear as name-only stubs)
- Ingest status/trigger stays on the existing jobs tooling, not bespoke routes.

`as` is a demo-trust parameter (single-household private tier); real per-user auth binding is deferred with the session work.

## 6. UI: `/app/grimoire`

One SvelteKit page (monolith frontend, private tier, standard /app/\* shared-origin conventions):

- Campaign picker (or default campaign), then a **viewpoint switcher**: DM or one of the party's characters.
- Entity browser: filter by type, name search; cards open scope-projected detail (a `partial` grant visibly shows only revealed details; `name_only` entities do not appear).
- Search box hitting `/search`: mixed entity + chunk results with scores.
- DM-only panel: grant editor (pick entity, character, scope) so the demo loop is "flip a grant as DM, switch to the player, see knowledge appear".

No live-session UI in v1. Namespaced localStorage keys per the /app/\* shared-origin convention.

## 7. Testing + CI

- Sibling `*_test.py` per module; **hand-add `py_test` targets** in the monolith BUILD (gazelle will not generate them).
- SQLite fixtures via `create_all`; CHECKs mirrored in `__table_args__`; pgvector-dependent search tested with a stub embedding client (dependency-override seam like `knowledge.api.get_embedding_client`).
- Core coverage: visibility predicate + scope projection (the product), loader idempotency + bad-line tolerance, extraction upsert/dedup, public-route allowlist unchanged.
- Frontend: mock-data visual regression only if cheap; not a gate for v1.
- No local test runs; push and watch BuildBuddy CI.

## 8. Loom-compat checklist (review gate for every schema PR)

- New queryable scalar => real column on a typed table, not a jsonb key.
- New entity kind => spine `entity_type` + (optionally deferred) detail table, never a polymorphic blob.
- New visibility behavior => expressed via `knowledge_grant` scope semantics, never a bespoke flag.
- New derived data => a Transform-shaped job with provenance columns, never inline mutation without lineage.
- Session-scoped mutation => carries a session id.
