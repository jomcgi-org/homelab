# Grimoire v1 (Postgres-First, Loom-Shaped) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task in this session. Repo overrides apply: no local test runs (write tests, verify by pushing and watching BuildBuddy CI at end of plan), one comprehensive code review per PR (not per task).

**Goal:** Ship a minimally functional Grimoire as a monolith domain module: Loom-shaped schema in monolith-pg, S3 chunk ingest + embeddings + OpenRouter entity extraction, grant-filtered lookup and vector search, and an /app/grimoire page demoing player-vs-DM visibility.

**Architecture:** Per [ADR 012](../decisions/services/012-grimoire-postgres-first-loom-shaped.md) and the [spec](2026-07-02-grimoire-pg-first-spec.md). Postgres is both system of record and hot tier; every shape mirrors the eventual Loom target (typed CTI entities, grant overlay as slice membership, one generic embedding surface with lineage columns, Transform-shaped batch jobs).

**Tech Stack:** FastAPI + SQLModel (monolith, ADR 010), pgvector via `shared/embedding.py` (voyage-4-nano, 1024), SeaweedFS S3, OpenRouter, SvelteKit (monolith frontend).

**Key repo gotchas for every implementer:**

- No local `pytest`/`bazel test`. Write tests; CI runs them on push.
- New `*_test.py` under `projects/monolith/` needs a **hand-added `py_test`** target in `projects/monolith/BUILD` (gazelle will not generate it; Format check passes green without it, then the test silently never runs).
- SQLite test fixtures use `create_all`; mirror Postgres CHECK constraints in `__table_args__`.
- Migrations live in `projects/monolith/chart/migrations/`; pick the next timestamp ID at implementation time (was `20260703060000` when planned). Keep migrations small (256KiB ConfigMap cap); no seed data in migrations.
- Chart changes require bumping `projects/monolith/chart/Chart.yaml` version AND `projects/monolith/deploy/application.yaml` `targetRevision` in the same commit.
- Conventional Commits; run `format` before each commit.
- Never write em-dashes in code comments, docs, or commit messages.

---

### Task 1: Schema migration + SQLModel models

**Files:**

- Create: `projects/monolith/chart/migrations/<next_id>_grimoire_schema.sql`
- Create: `projects/monolith/grimoire/__init__.py`
- Create: `projects/monolith/grimoire/models.py`
- Create: `projects/monolith/grimoire/models_test.py`
- Modify: `projects/monolith/BUILD` (hand-add `py_test` for `models_test`)

**Step 1: Migration.** Create schema `grimoire` and the tables from spec §3, exactly:

- `entity` spine (`id` uuid pk default gen_random_uuid(), `entity_type` text CHECK IN (creature,spell,location,npc,faction,deity,item), `name` text not null, `source_type` text CHECK IN (extracted,homebrew) default 'extracted', `is_global` bool default true, `source_book` text, `created_in_session` uuid, `created_at` timestamptz default now()); index on `(entity_type, name)`.
- Detail tables `entity_creature`, `entity_spell`, `entity_location`, `entity_npc` (pk = `entity_id` uuid references `grimoire.entity(id)` on delete cascade; columns per spec §3.1).
- `knowledge_chunk` (unique `(book_id, chunk_ref)`), `chunk_entity_mention` (unique `(chunk_id, entity_id)`), `relationship` (unique `(from_entity_id, to_entity_id, rel_type)`).
- `embedding` (`embeddable_kind` text CHECK IN (entity,chunk,transcript), `embeddable_id` uuid, `model` text, `dim` int, `vector` vector(1024), unique `(embeddable_kind, embeddable_id, model)`). `CREATE EXTENSION IF NOT EXISTS vector` is already handled by existing knowledge migrations; do not re-create.
- `campaign`, `player_character`, `game_session` (partial unique index: `CREATE UNIQUE INDEX ... ON grimoire.game_session (campaign_id) WHERE status != 'ended'`), `knowledge_grant` (`grant_scope` CHECK IN (full,partial,name_only), unique `(entity_id, player_character_id)`).

**Step 2: Models.** `models.py`: SQLModel classes matching the migration 1:1, `__table_args__ = {"schema": "grimoire"}` plus mirrored CHECKs (follow the pattern used by an existing domain with CHECKs; grep `CheckConstraint` under `projects/monolith/`). For SQLite fixtures, schema-qualified tables need the existing pattern other domains use (grep how `knowledge` handles schema in tests; if fixtures strip schemas via `create_all` on an attached metadata, follow that exactly).

**Step 3: Tests.** `models_test.py`: create_all fixture; insert an entity + creature detail + embedding row + grant; assert CHECK violation raises for a bad `grant_scope`; assert the unique `(entity_id, player_character_id)` constraint. Assert at most one active session per campaign at the app level (the partial index is Postgres-only; add the app-level guard in Task 3 and test it there).

**Step 4: BUILD.** Hand-add the `py_test` target mirroring an existing sibling (e.g. the campsites test target).

**Step 5: Commit** `feat(grimoire): schema migration + models for loom-shaped postgres tier`.

---

### Task 2: Visibility helper (the product core)

**Files:**

- Create: `projects/monolith/grimoire/visibility.py`
- Create: `projects/monolith/grimoire/visibility_test.py`
- Modify: `projects/monolith/BUILD`

**Step 1: Implement** `visibility.py` with two functions used by every read path:

- `visible_entities_query(session, campaign_id, viewer)` where viewer is `"dm"` or a `player_character_id`: DM returns unfiltered select over `entity`; player returns entities where `is_global == True` OR a grant row exists for `(entity_id, viewer)`. Return `(entity, grant_or_none)` pairs (LEFT JOIN), the ADR 011 predicate:

```sql
SELECT e.*, g.grant_scope, g.revealed_details
FROM grimoire.entity e
LEFT JOIN grimoire.knowledge_grant g
  ON g.entity_id = e.id AND g.player_character_id = :me
WHERE e.is_global OR g.id IS NOT NULL
```

- `project_entity(entity, detail_row, grant, viewer) -> dict | None`: DM gets everything + grant annotations; `full` (or `is_global` with no grant) gets spine + typed detail; `partial` gets spine fields + `revealed_details` only; `name_only` returns `None` for direct lookup/search paths (recognition-only rule) and a `{"id", "name", "entity_type", "recognition_only": true}` stub for the relationship-context path (flag argument `context="relationship"`).

**Step 2: Tests.** Fixture: one campaign, two PCs, entities covering all four cases (global, full-granted, partial-granted, name_only-granted, ungranted). Assert per viewer: visible set, projection contents (partial shows only revealed_details, not detail columns), name_only excluded from lookup but stubbed in relationship context, DM sees all, ungranted invisible to players.

**Step 3: BUILD + commit** `feat(grimoire): grant-overlay visibility predicate and scope projection`.

---

### Task 3: Module registration + campaign/character/grant CRUD

**Files:**

- Create: `projects/monolith/grimoire/router.py`
- Modify: `projects/monolith/grimoire/__init__.py` (export `register`, `on_startup_jobs` no-op for now)
- Modify: `projects/monolith/app/main.py` (call `grimoire.register(app)` alongside existing domains)
- Create: `projects/monolith/grimoire/router_test.py`
- Modify: `projects/monolith/BUILD`

**Step 1: Router.** `router = APIRouter(prefix="/api/grimoire", tags=["grimoire"])`. Endpoints per spec §5: campaigns CRUD (POST/GET list/GET one), characters (POST/GET under campaign), grants (POST create, GET list, PATCH scope/revealed_details by grant id). Session creation `POST /campaigns/{id}/sessions` enforcing the single-active-session invariant at the app level (409 on second active). Pydantic response models, never ORM objects across the boundary (ADR 010).

**Step 2: Registration.** `__init__.py` exports `register(app)` importing the router lazily (match `ships/__init__.py`). No `register_public`. Wire into `app/main.py` private binary only.

**Step 3: Tests.** FastAPI TestClient over the create_all fixture: create campaign -> characters -> grants; PATCH a grant `partial -> full`; second active session 409s. Confirm `main_public_routes_test.py` still passes conceptually (no public registration; do not edit ALLOWED_PREFIXES).

**Step 4: BUILD + commit** `feat(grimoire): private-tier module registration and campaign CRUD`.

---

### Task 4: Grant-filtered entity lookup + relationships

**Files:**

- Modify: `projects/monolith/grimoire/router.py`
- Create: `projects/monolith/grimoire/entities_test.py`
- Modify: `projects/monolith/BUILD`

**Step 1:** Endpoints per spec §5 using `visibility.py` exclusively:

- `GET /campaigns/{id}/entities?as=&type=&q=` (name ILIKE on `q`; `name_only` rows excluded).
- `GET /campaigns/{id}/entities/{entity_id}?as=` (project with typed detail; non-visible and `name_only` both return 404 so existence does not leak).
- `GET /campaigns/{id}/entities/{entity_id}/relationships?as=` (1-hop edges over `relationship`; neighbor entities projected, `name_only` neighbors as recognition stubs).

**Step 2: Tests** reusing Task 2's fixture through the HTTP layer: player vs DM lists differ; 404 on ungranted and on name_only direct lookup; relationship endpoint shows the name_only stub.

**Step 3: BUILD + commit** `feat(grimoire): grant-filtered entity lookup and relationship endpoints`.

---

### Task 5: Chunk loader (S3 -> knowledge_chunk + embedding)

**Files:**

- Create: `projects/monolith/grimoire/ingest.py`
- Create: `projects/monolith/grimoire/ingest_test.py`
- Modify: `projects/monolith/BUILD`

**Step 1:** `ingest.py`:

- `parse_manifest_lines(book_id, lines) -> (valid_chunks, error_count)`: NDJSON per spec §4.1 (`chunk_ref`, `content` required; `section_path`, `meta` optional); bad lines counted and logged, never raised.
- `async load_chunks(session, s3_client, embed_client, bucket, prefix="chunks/")`: list `*.ndjson`, book_id = filename stem, upsert `knowledge_chunk` on `(book_id, chunk_ref)` (skip unchanged content), batch-embed new/changed via `EmbeddingClient.embed_batch`, upsert `embedding` rows (`embeddable_kind="chunk"`, record `model` + `dim`). Return a summary dict (books, chunks upserted, embedded, errors).
- S3 access: reuse the monolith's existing S3 client/credentials pattern (grep how `chat` blobs or `trips` access SeaweedFS; use the same env/client construction).

**Step 2: Tests.** Stub S3 (in-memory listing/get) and stub embedding client returning fixed vectors. Assert: idempotent re-run embeds nothing new; changed content re-embeds; bad NDJSON line skipped with `errors == 1`; `(book_id, chunk_ref)` uniqueness holds.

**Step 3: BUILD + commit** `feat(grimoire): s3 chunk loader with idempotent embedding`.

---

### Task 6: Entity extraction via OpenRouter

**Files:**

- Create: `projects/monolith/grimoire/extract.py`
- Create: `projects/monolith/grimoire/extract_test.py`
- Modify: `projects/monolith/BUILD`

**Step 1:** `extract.py`:

- A thin OpenRouter client: `POST https://openrouter.ai/api/v1/chat/completions` (OpenAI-compatible), model from `GRIMOIRE_EXTRACT_MODEL` env, key from `OPENROUTER_API_KEY`, `response_format={"type": "json_object"}`, retry/backoff mirroring `shared/embedding.py`'s discipline.
- Prompt: given chunk text, emit `{"entities": [{"entity_type", "name", "detail": {...typed fields...}, "summary"}], "mentions": [...], "relationships": [{"from_name", "to_name", "rel_type"}]}`. Keep the prompt a module constant so iteration is a one-line diff.
- `async extract_chunks(session, or_client, embed_client, limit)`: select chunks with no `chunk_entity_mention` rows (bounded by `limit`), call the model, upsert entities by `(entity_type, name)` (name-dedup per ADR 012 open question 2), write detail rows for the four typed tables (spine-only for faction/deity/item), insert mentions + relationships (resolving names against existing entities), embed new entities (`name + summary`, `embeddable_kind="entity"`). Malformed model output for a chunk: log, skip, count; never fail the run.

**Step 2: Tests.** Stub OpenRouter client returning canned JSON. Assert: entities + typed detail rows created; re-running on the same chunk is a no-op (mentions exist -> not selected); name-dedup reuses an existing entity; malformed JSON counted as error; relationship rows unique.

**Step 3: BUILD + commit** `feat(grimoire): openrouter entity extraction from chunks`.

---

### Task 7: Jobs, secrets, values, chart bump

**Files:**

- Create: `projects/monolith/grimoire/jobs.py`
- Modify: `projects/monolith/grimoire/__init__.py` (`on_startup_jobs` registers both jobs)
- Modify: monolith chart secret templates (OnePasswordItem for the OpenRouter key; follow the existing 1Password item pattern in `projects/monolith/chart/`)
- Modify: `projects/monolith/deploy/values.yaml` + chart env plumbing: `OPENROUTER_API_KEY` (secretRef), `GRIMOIRE_S3_BUCKET=grimoire`, `GRIMOIRE_EXTRACT_MODEL`
- Modify: `projects/monolith/chart/Chart.yaml` + `projects/monolith/deploy/application.yaml` (version + targetRevision together)
- Create: `projects/monolith/grimoire/jobs_test.py`; Modify: `projects/monolith/BUILD`

**Step 1:** `jobs.py`: `grimoire_load_chunks` and `grimoire_extract_entities` handlers wrapping Task 5/6 functions, registered via the existing scheduler registration pattern (grep `on_startup_jobs` implementations; mark extraction `heavy` if that flag exists per scheduler conventions). Not on any aggressive cron; manual trigger via existing tooling, a daily tick is fine if the registry requires an interval.

**Step 2:** Secrets + values plumbing; render locally with `helm template monolith projects/monolith/chart/ -f projects/monolith/deploy/values.yaml` and eyeball the env + OnePasswordItem output.

**Step 3:** Note in the PR body: the `grimoire` S3 bucket must exist (create per the existing SeaweedFS bucket process); the 1Password item `openrouter` must exist with the key field before sync.

**Step 4: Tests** for job handlers (wiring-level: handler calls loader with configured bucket; stub everything external).

**Step 5: Commit** `feat(grimoire): ingest jobs, openrouter secret, chart plumbing`.

---

### Task 8: Vector search endpoint

**Files:**

- Create: `projects/monolith/grimoire/search.py`
- Modify: `projects/monolith/grimoire/router.py` (`GET /campaigns/{id}/search?as=&q=&k=`)
- Create: `projects/monolith/grimoire/search_test.py`
- Modify: `projects/monolith/BUILD`

**Step 1:** `search.py`: embed the query via the injected embedding client (reuse the `get_embedding_client` dependency-seam pattern from `knowledge/api.py`), kNN over `embedding` by cosine distance (over-fetch `4k`), resolve hits: chunk hits always visible (corpus is global in v1), entity hits filtered + projected through `visibility.py` (name_only dropped), trim to `k`, return mixed results `{kind, id, name/preview, score}`.

**Step 2: Tests.** SQLite has no pgvector: keep the kNN query in one small function and test it Postgres-only via CI if an existing pattern exists (grep how `knowledge` tests vector queries); otherwise stub the kNN function and test the filter/projection/trim logic around it. Do not skip testing the visibility filtering.

**Step 3: BUILD + commit** `feat(grimoire): grant-filtered vector search across entities and chunks`.

---

### Task 9: /app/grimoire SvelteKit page

**Files:**

- Create: `projects/monolith/frontend/src/routes/app/grimoire/+page.svelte` (+ `+page.ts` load, api client module) following an existing private /app page's structure (pick the closest private-tier app page as the template)
- Modify: frontend route/nav registration if the app index lists apps

**Step 1:** Implement spec §6: campaign picker, viewpoint switcher (DM | party characters), entity browser (type filter, name search, detail modal showing scope-projected fields), search box (mixed results), DM grant editor (entity + character + scope dropdowns, POST/PATCH). Namespaced localStorage keys (`grimoire:*`). Match existing frontend conventions (fetch wrapper, styling, error handling); keep it one page, no new dependencies (remember `ssr.noExternal` if any dep is added).

**Step 2:** `pnpm` type-check/build via the standard frontend checks (beware: macOS `pnpm build` can clobber BUILD files; use the repo's sanctioned frontend check command).

**Step 3: Commit** `feat(grimoire): /app/grimoire visibility demo page`.

---

### Task 10: Format, push, CI, review, merge

**Steps:**

1. `format` across the worktree; fix anything it flags; verify BUILD contains a hand-added `py_test` per new test file (list them and diff against BUILD).
2. Push `feat/grimoire-pg-first`, open the PR (docs from the earlier commits + implementation), body summarizing ADR 012 + spec + the two manual prerequisites (S3 bucket, 1Password item).
3. Watch `gh pr checks --watch`; on failure, read logs via `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`, quote the failure verbatim, fix, push.
4. One comprehensive Opus code review of the full diff (repo cadence: per-PR, not per-task).
5. Merge with `gh pr merge --rebase` after green + review; verify ArgoCD sync and that migrations applied (Atlas), then trigger `grimoire_load_chunks` once a manifest lands in the bucket.

---

## Verification (definition of minimally functional)

After merge + deploy + one loaded book + one extraction run:

1. Create a campaign and two PCs via `/api/grimoire`.
2. As DM: entities list shows extracted creatures/spells/locations/NPCs.
3. Grant PC1 `full` on one creature, `partial` (revealed_details `{"note": "seen at night"}`) on an NPC, `name_only` on a location.
4. As PC1: creature fully visible; NPC shows only the note; location absent from lookup/search but named in a relationship view; everything else global-only.
5. `/campaigns/{id}/search?as=<pc1>&q=...` returns mixed chunk + entity hits respecting the above.
6. Same loop through `/app/grimoire` by switching viewpoints.
