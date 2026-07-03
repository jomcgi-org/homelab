# Grimoire UI Overhaul: Critique, Spec, and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. One comprehensive code review per PR, at the end of each phase (repo policy).

**Goal:** Turn `/app/grimoire` from a CRUD admin panel into a readable, explorable D&D reference: clean desktop and mobile UX, a first-class surface for browsing book chunks as ingestion and extraction flow in, and typeset entity presentation.

**Architecture:** Three PR-sized phases. Phase 1 restructures the frontend (URL routing, responsive master-detail, one search). Phase 2 adds the read path the API is missing (books, sections, chunk reader, entity-to-chunk provenance, image serving) plus the Library UI. Phase 3 is presentation: per-type entity renderers (stat blocks), contextual grant UX, and a Grimoire-specific visual identity layered on the monolith token system.

**Tech Stack:** SvelteKit (Svelte 5 runes, `ssr = false` client-fetch pattern), FastAPI + SQLModel (`projects/monolith/grimoire/`), Atlas migrations, existing `/api/grimoire` HTTPRoute rule (all new endpoints stay under this prefix, no HTTPRoute change needed).

**Status quo baseline:** main at `c40c7b434` (Marker ingest #3133, chunk `image_ref` column, embedding batch fix #3144 all merged). First book loaded: D&D Monster Manual, 1224 chunks (915 text, 309 image).

---

## Design Context

No `.impeccable.md` exists yet; these assumptions were confirmed by the brief ("clean UX for desktop and mobile", "good exploration of chunks as they flow in", "boring + hard to use") and should be revisited if wrong.

- **Audience:** Joe as DM plus his players. Two usage postures: DM prepping or running a session (desktop, reading-heavy), players checking what has been revealed to them (mostly phones, couch context).
- **Jobs:** read monster/spell/location lore; watch a newly uploaded book get extracted; control per-character knowledge grants; share "look at this" links at the table.
- **Tone:** keep the monolith house skeleton (light, hairline borders, mono chrome) but give Grimoire its own identity: an arcane ledger, not a terminal. One memorable artifact: a properly typeset Monster Manual style stat block.

## Critique Summary (what is wrong today)

The full UI is one 1090-line `+page.svelte` with three equal-weight stacked panels (search, detail, grant editor) beside a fixed 22rem entity sidebar.

1. **Chunks have no home.** They are reachable only as truncated vector-search previews titled `{book_id UUID} · {section_path}`. No book list, no section browser, no full-content reader, no images (309 image chunks invisible), no freshness signal while the extraction CronWorkflow churns (~49 runs per book). `chunk_entity_mention` provenance exists in Postgres and is exposed by no endpoint.
2. **Mobile is broken.** Zero media queries; `22rem` fixed sidebar in a no-wrap flex row inside `100vh`; sub-44px touch targets.
3. **No URLs.** Selected entity, campaign, and viewpoint are ephemeral `$state`; refresh loses your place; nothing is shareable; back button is dead.
4. **Hero content renders as `JSON.stringify`.** Typed CTI detail models (creature, spell, location, npc) exist server-side, then the UI flattens them to key-value rows and raw `<pre>` JSON.
5. **Two unexplained search boxes, permanent grant panel.** Sidebar name filter vs semantic search; DM pays a full-time screen-space tax for grant admin; partial grants require hand-written JSON in a textarea.
6. **Anonymous, not ugly.** House style with zero domain flavor; raw cosine scores in the UI; dead-end empty states ("no entities visible"); unpaginated entity list.

What works and must be kept: the DM/character viewpoint switcher (top bar, localStorage), relationship rows with direction arrows and recognition-only dimming, debounced semantic search plumbing.

---

## Target UX Spec

### Navigation and routes

```
/app/grimoire                            redirect to last campaign (localStorage) or campaign picker
/app/grimoire/[campaign]                 Library (books + coverage, "new since last visit")
/app/grimoire/[campaign]/entities        entity index (filterable, paginated)
/app/grimoire/[campaign]/entity/[id]     entity detail (stat block, provenance, grants)
/app/grimoire/[campaign]/book/[book]     section tree for one book
/app/grimoire/[campaign]/book/[book]/c/[chunk]  chunk reader (full content or image, prev/next)
```

Viewpoint stays a query param (`?as=dm` or `?as=<character-id>`) so every URL is shareable in-context. A shared `+layout.svelte` owns the top bar (campaign select, viewpoint switcher, omnibox) and the responsive frame.

### Responsive frame

- **Desktop (>= 880px):** two panes. Left: contextual list (entity index, section tree, or search results). Right: reading pane (entity detail or chunk reader). No third panel; grants are contextual.
- **Mobile (< 880px):** master-detail stack. List routes and detail routes are separate screens with back navigation (real history back, since state is now in URLs). `100dvh`, minimum 44px touch targets, thumb-reachable back/next controls in the chunk reader.

### Search

One omnibox in the top bar replaces both boxes. On type: instant name matches (existing `q` name filter) render first as "Entities", then debounced semantic results as "Lore" (chunk hits) and "Related entities" (vector entity hits). No raw scores; order communicates relevance. Chunk hits show book display name + section title, never UUIDs.

### Library and chunk exploration (the headline)

- **Library:** one card-free row per book: display name, chunk counts (text/image), extraction coverage ("915 / 1224 chunks extracted"), entity yield, last-loaded time, and a "new" marker computed against a per-device `grimoire:lastSeen:<book>` localStorage timestamp. This is the surface that makes "flowing in" visible: coverage ticks up as the CronWorkflow runs.
- **Section tree:** ordered list of `section_path` groups with chunk counts and new-chunk badges. Section order follows chunk sequence, not alphabetical.
- **Chunk reader:** full `content` with reading typography (measure ~65ch, not mono), image chunks render the actual image with the LLM caption underneath, prev/next within the book sequence, "entities on this page" chips (from `chunk_entity_mention`, viewpoint-filtered) linking into entity detail.
- **Provenance both ways:** entity detail gains a "Sources" section listing the chunks that mention it, each linking into the reader.

### Entity presentation

Per-type renderers keyed on `entity_type`, falling back to the generic field list for types without one:

- **creature:** Monster Manual style stat block (name, size/type/alignment strap, AC/HP/speed row, ability score table, traits/actions prose). Tapered rule dividers, small-caps section heads.
- **spell:** spell card (level/school strap, casting time/range/components/duration grid, description).
- **npc / location / faction / deity / item:** typed field layouts, prose-first.

`revealed_details` for partial grants renders through the same renderer over the revealed subset, never as JSON.

### Grants UX (DM only)

- Grant controls live on entity detail: one row per campaign character with scope chips (`none / name only / partial / full`). Tapping a chip creates or PATCHes the grant.
- Partial reveals get a field picker (checkboxes over the entity's populated fields) that builds `revealed_details` server-side-compatible JSON; the raw JSON textarea dies.
- The standalone grant panel becomes a read-only "Reveals" review list on the campaign page (what each character can see), reachable but not permanently on screen.

### Visual identity

Scoped to the grimoire route tree, layered over shared tokens (do not fork the token system):

- **Type:** keep `--font-mono` for chrome (labels, badges, nav). Add one self-hosted display serif for entity names, stat block heads, and book/section titles (self-hosting rule per `global.css`; pick a licensed open serif with small caps, e.g. Vollkorn or Spectral, decided in the task).
- **Color:** grimoire accent `--grim-accent` in the oxblood/madder family (MM stat-block red heritage) replacing `#0066ff` inside the app; tinted near-white paper background for the reading pane only.
- **Micro-moments:** staggered fade-in for extraction coverage counts on the Library, "new" badges, stat block divider flourish. No layout-property animation; transform/opacity only; respect `prefers-reduced-motion`.
- **Empty states teach:** player with no reveals sees "The DM has not revealed anything to <name> yet"; empty library links to `tools/upload-book.sh` usage; loading states are skeleton lines, not "loading...".

### Explicit non-goals (this plan)

Live session state machine / realtime / voice (deferred per ADR 012 follow-ups), entity resolution across books, campaign scoping of the global corpus, editing entities from the UI, offline support.

---

## API Additions (Phase 2 backend)

All under the existing `/api/grimoire` prefix. Chunks are corpus-global in v1 (matching `search._resolve_chunk_hit`), so book/chunk endpoints are not campaign-scoped; mention projections are.

| Endpoint                                              | Returns                                                                                                               |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| `GET /books`                                          | `[{book_id, display_name, chunk_count, image_count, extracted_count, entity_count, last_loaded_at, latest_chunk_at}]` |
| `GET /books/{book_id}/sections`                       | ordered `[{section_path, title, chunk_count, image_count, first_chunk_id, latest_chunk_at}]`                          |
| `GET /books/{book_id}/chunks?section=&cursor=&limit=` | seq-ordered page of `{id, seq, section_path, kind: text                                                               | image, preview, created_at}` |
| `GET /chunks/{chunk_id}?campaign=&as=`                | full `{content, image_url, section_path, seq, prev_id, next_id, entities: [projected mentions]}`                      |
| `GET /chunks/{chunk_id}/image`                        | streams the S3 object behind `image_ref` (private tier auth applies)                                                  |
| `GET /campaigns/{id}/entities/{eid}/mentions?as=`     | `[{chunk_id, book_id, section_path, mention_text, preview}]`                                                          |
| `GET /campaigns/{id}/entities` (existing)             | gains `limit`/`cursor` pagination + `total`                                                                           |

**Ordering:** chunk order within a book must be reading order. `created_at` insertion order is unreliable (bulk upserts share timestamps; re-uploads mutate rows in place). Add a `seq` integer to `knowledge_chunk` populated from NDJSON line order at ingest.

**Display names:** `book_id` is a UUID today. Add `grimoire.book` metadata table (`id`, `display_name`, `created_at`); loader upserts a row per book (display_name defaults to the id until set); expose `PATCH /books/{book_id}` to rename from the Library UI.

**Image serving decision (simplest first):** stream through the monolith backend with boto3 (creds already in the workflow/api env for the loader; verify the API deployment has the S3 secret, mirror the Kyverno secret-clone if not). imgproxy resizing is a later optimization; record it as a follow-up, do not build it now.

---

## Implementation Plan

Every phase is one worktree, one PR, one end-of-PR review, CI on push (no local test loop; repo policy). Backend tasks are TDD against the SQLite `create_all` fixtures (new `*_test.py` targets need hand-added `py_test` rules in `projects/monolith/BUILD`, per repo memory). Frontend verification is `format` + CI type-check + manual against prod data (grimoire is private tier, so the public visual-regression rig does not apply). Migration numbering: check the applied head and the `Check migration version ordering` hook before picking a version.

### Phase 1 PR: structure (routing, responsive, one search)

**Task 1.1: route skeleton + shared layout.**
Files: create `frontend/src/routes/private/app/grimoire/[campaign]/+layout.svelte`, `+layout.ts` (`ssr = false`), move existing page logic into it; create `[campaign]/entities/+page.svelte`, `[campaign]/entity/[id]/+page.svelte`; `grimoire/+page.svelte` becomes the redirect/picker.
Steps: extract `apiFetch`, campaign/character/viewpoint loading into `frontend/src/lib/grimoire/api.js` + a layout-level context; wire `?as=` query param as the viewpoint source of truth (localStorage only seeds the default); keep DM fallback semantics identical to `loadCharacters` today. Commit per route added.

**Task 1.2: responsive master-detail frame.**
Files: layout + new `frontend/src/lib/grimoire/Shell.svelte`.
Steps: two-pane grid >= 880px; below it, list and detail routes render standalone with a back affordance; replace `100vh` with `100dvh`; audit every button/input for 44px min hit area. Verify with browser device emulation at 390px and 1280px.

**Task 1.3: omnibox.**
Files: `frontend/src/lib/grimoire/Omnibox.svelte`; delete the sidebar name filter and the search panel.
Steps: instant name matches via existing `entities?q=` (keep 150ms debounce), semantic via existing `/search` (300ms debounce); grouped results (Entities / Lore / Related); drop score display; keyboard navigation (arrows + enter); Escape closes. Entity type filter becomes chips on the entity index page.

**Task 1.4: pagination + empty states.**
Files: `router.py` `list_entities` (add `limit`, `cursor`, `total`; default limit 100, ordered by name), `router_test.py`; entity index infinite scroll or "load more".
Steps: failing test for pagination shape first, then implement; player empty state copy ("The DM has not revealed anything to <character> yet"); loading skeletons replace "loading..." text. Push, watch `gh pr checks`, fix, end-of-PR review, `gh pr merge --auto --rebase`.

### Phase 2 PR: the Library (chunk exploration)

**Task 2.1: migration + models: `seq` and `book` table.**
Files: new `chart/migrations/<next>_grimoire_chunk_seq_and_book.sql` (add `seq int` to `grimoire.knowledge_chunk`, backfill from `created_at, chunk_ref` order per book; create `grimoire.book`), `models.py`, `models_test.py`.
Steps: write model tests (SQLite fixtures use `create_all`, no migrations); update `ingest.py` `parse_manifest_lines`/upsert to assign `seq` from line order and upsert `book` rows; `ingest_test.py` asserts seq stability across re-upload. Remember the version-ordering CI guard.

**Task 2.2: read endpoints.**
Files: `router.py`, new `library.py` (aggregation queries), `library_test.py`, `router_test.py`.
Steps: TDD each endpoint from the API table above. `extracted_count` = distinct chunk_ids in `chunk_extraction` with status success/empty for the current (model, prompt_hash); reuse the marker-table semantics from `extract.py`. `entity_count` = distinct entities via `chunk_entity_mention` join. Mentions endpoint projects through `visibility.py` helpers (grant predicate stays in one place). Hand-add `py_test` targets in BUILD.

**Task 2.3: image streaming endpoint.**
Files: `router.py` (`GET /chunks/{chunk_id}/image`), `router_test.py` (mock S3 client), values/env check for the API deployment's S3 creds.
Steps: parse `image_ref` (`s3://bucket/key`), stream with correct content-type, 404 on text chunks; `StreamingResponse` with async wrapper per the module's `asyncio.to_thread` session conventions; verify the deployed API pod env actually has the SeaweedFS creds before merging (values change if not, remember chart bump policy: Chart.yaml + application.yaml targetRevision together).

**Task 2.4: Library, section tree, chunk reader UI.**
Files: `[campaign]/+page.svelte` (Library), `[campaign]/book/[book]/+page.svelte`, `[campaign]/book/[book]/c/[chunk]/+page.svelte`, `frontend/src/lib/grimoire/ChunkReader.svelte`.
Steps: Library rows with coverage counts and per-book "new since last visit" (localStorage timestamp vs `latest_chunk_at`); section list ordered by seq; reader with prose typography (`max-width: 65ch`, non-mono body), image rendering via the streaming endpoint, prev/next (preload neighbor), entity chips linking to entity detail.

**Task 2.5: provenance on entity detail.**
Files: `entity/[id]/+page.svelte` "Sources" section using the mentions endpoint; search chunk hits now link into the reader route and show book display name + section title.
End-of-PR review, auto-merge, verify rollout (kubectl) and click through on prod data.

### Phase 3 PR: presentation (stat blocks, grants, identity)

**Task 3.1: per-type entity renderers.**
Files: `frontend/src/lib/grimoire/statblock/Creature.svelte`, `Spell.svelte`, `Generic.svelte` + a `EntityDetail.svelte` dispatcher keyed on `entity_type`.
Steps: render from the typed detail fields (inspect `EntityCreature`/`EntitySpell` models for the exact columns); `revealed_details` subsets flow through the same renderers; JSONB overflow fields render as labeled prose, never raw JSON. This task carries the visual bar for the whole app; review screenshots at 390px and 1280px before commit.

**Task 3.2: contextual grant editor.**
Files: `entity/[id]/+page.svelte` grant section (DM only), `frontend/src/lib/grimoire/GrantChips.svelte`; campaign-page "Reveals" review list; delete the old grant panel.
Steps: per-character scope chips (none/name only/partial/full) calling existing POST/PATCH grant endpoints; partial opens a field picker over populated detail fields building `revealed_details`; optimistic update with rollback on error.

**Task 3.3: Grimoire identity layer.**
Files: `frontend/src/lib/grimoire/theme.css` (scoped custom properties), self-hosted serif woff2 under `frontend/static/fonts/` wired like existing fonts in `global.css` (check the font-loading gotcha comment there), motion touches.
Steps: choose and license-check the serif; `--grim-accent` oxblood replaces the blue accent within grimoire routes; paper-tint reading pane; staggered reveal on Library coverage counts; `prefers-reduced-motion` guard; microcopy pass over every empty/error state.

**Task 3.4: polish + review.**
Steps: keyboard/focus audit, `format`, end-of-PR review against this spec, push, CI, auto-merge, rollout verification, update `.impeccable.md` proposal: after Phase 3 ships, run teach-impeccable to persist the design context this plan assumed.

---

## Risks and open questions

- **Backfill correctness of `seq`:** `created_at, chunk_ref` ordering approximates reading order for the already-loaded MM; a re-upload after Task 2.1 rewrites seq from true line order. Acceptable: the MM can simply be re-pushed with `tools/upload-book.sh`.
- **S3 creds on the API pod:** the loader runs in Argo workflows with its own env; the API deployment may lack the SeaweedFS secret. Task 2.3 verifies before merge (Kyverno clone pattern exists if needed).
- **Corpus growth:** endpoints are paginated and aggregates are per-book GROUP BYs; fine at 10^3..10^4 chunks per book. Revisit indexes (`knowledge_chunk(book_id, seq)`) in the Task 2.1 migration.
- **Viewpoint spoofing is out of scope:** `?as=` is convenience, not auth (private tier already gates access); unchanged from today's model.
