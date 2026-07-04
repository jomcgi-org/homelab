# Grimoire Public Read-Only Tier

**Goal:** Ship the redesigned Grimoire as a public, read-only, link-shareable app in the monolith public tier, reusing the existing public design system (`lib/public/styles/design-system.css` + `lib/public/components`), so friends can browse and read the loaded sourcebook during the demo period without auth. The private `/private/app/grimoire` (grants/DM tooling) stays untouched.

**Decisions already locked (do not relitigate):**

- Adopt the public design system as-is: yellow `--accent #ffde01`, `--blue #6fc2ff` for primary buttons, cream/paper/ink neutrals, Instrument Serif (`.display`) + Hanken Grotesk (`--sans` body) + JetBrains Mono (`--mono` labels), rounded `--radius`, `.card-hard`, `.btn-primary/.btn-secondary`, `.hl-yellow`, `Nav`, `Footer`, `BrutalistSelect`, `Marquee`, `Sticker`, `Seo`. Near-zero custom CSS.
- Exposure: public route behind `TurnstileGate` + `noindex` (robots disallow). Link-shareable, not crawlable.
- Scope: full read surface in one PR (Library, sections, chunk reader, entities index, entity stat blocks).
- Corpus is WotC-copyrighted (D&D Monster Manual); noindex + Turnstile is the mitigation, accepted for the demo period.

**Tech context (verified):** public tier = `monolith-public` chart + `app/main_public.py` (mounts each domain's `register_public`, reads `monolith-pg-ro` as role `public_reader`). Public frontend routes live under `frontend/src/routes/public/app/<name>/`, `ssr=false`, client-fetch to `${API_BASE}/api/...`. `library.py` book/section/chunk functions are already corpus-global (grant-agnostic) and reused directly; only the entity path is net-new for the no-grants full view.

---

## Architecture

### Public URL structure (no campaign, no viewpoint)

```
/app/grimoire                          Library (books + coverage, corpus ticker)
/app/grimoire/book/[book]              section tree for a book
/app/grimoire/book/[book]/c/[chunk]    chunk reader (prev/next, structural render)
/app/grimoire/entities                 entity index (type filter + search)
/app/grimoire/entity/[id]              entity detail (stat block, sources, relationships)
```

No `[campaign]` segment and no `?as=`: the public corpus is a single global view with everything visible.

### Public API contract (served by `router_public`, mounted ONLY in `main_public`)

All under `/api/grimoire`. No `campaign`/`as` params anywhere. Reuses the private paths deliberately: the public router is never mounted in the private app, so there is no route collision.

| Endpoint                                | Returns                                                                                                                                                                                                       |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /books`                            | `[{book_id, display_name, chunk_count, image_count, extracted_count, entity_count, last_loaded_at, latest_chunk_at}]` (reuse `library.list_books`)                                                            |
| `GET /books/{book_id}/sections`         | ordered `[{section_path, title, chunk_count, image_count, first_chunk_id, latest_chunk_at}]` (reuse `library.list_sections`)                                                                                  |
| `GET /chunks/{chunk_id}`                | `{id, book_id, content, section_path, seq, chunk_count, image_url, prev_id, next_id, entities:[{id, name, entity_type, mention_text}]}` (new `public.get_chunk_public`: ALL mentions, unprojected, no grants) |
| `GET /chunks/{chunk_id}/image`          | streams the S3 object behind `image_ref` (reuse the private streaming impl)                                                                                                                                   |
| `GET /entities?type=&q=&limit=&cursor=` | `{items:[{id, name, entity_type, <secondary>}], total, next_cursor}` (new: all entities, no grants; `<secondary>` = size/CR for creature, level/school for spell, joined from typed detail)                   |
| `GET /entities/{id}`                    | full spine + typed detail flattened (`{id, name, entity_type, ...creature/spell/location/npc fields}`) (new: no grants)                                                                                       |
| `GET /entities/{id}/mentions`           | `[{chunk_id, book_id, section_path, preview}]` (reuse `library.list_entity_mentions`)                                                                                                                         |
| `GET /entities/{id}/relationships`      | `[{direction, rel_type, entity:{id, name, entity_type}}]` (new: all edges, no recognition dimming)                                                                                                            |
| `GET /search?q=`                        | `{entities:[{id,name,entity_type}], lore:[{chunk_id, book_id, display_name, section_path, preview}]}` (new/reuse: name + semantic, no grant filter)                                                           |

---

## Tasks

### Task 1 - Backend: `public_reader` GRANT migration

- New `chart/migrations/20260704000000_grimoire_public_reader_grant.sql` (head is `20260703250000`; confirm with the `Check migration version ordering` hook before finalizing):
  ```sql
  GRANT USAGE ON SCHEMA grimoire TO public_reader;
  GRANT SELECT ON grimoire.entity, grimoire.entity_creature, grimoire.entity_spell,
                  grimoire.entity_location, grimoire.entity_npc,
                  grimoire.knowledge_chunk, grimoire.chunk_entity_mention,
                  grimoire.relationship, grimoire.embedding
      TO public_reader;
  ALTER DEFAULT PRIVILEGES IN SCHEMA grimoire GRANT SELECT ON TABLES TO public_reader;
  ```
- Do NOT grant campaign/player_character/game_session/knowledge_grant (stay private).
- Keep it small (out of the 256 KiB last-applied-config cap; it is tiny). No test (migration).

### Task 2 - Backend: public read module + router

- New `grimoire/public.py`: pure read functions, no campaign/viewer/grant args.
  - `get_chunk_public(session, chunk_id)` -> mirror `library.get_chunk` but list ALL `chunk_entity_mention` rows as `{id, name, entity_type, mention_text}` with no projection; include `chunk_count`, `prev_id`, `next_id`, `image_url`.
  - `list_entities_public(session, entity_type, q, limit, cursor)` -> all entities ordered by name, paginated `{items, total, next_cursor}`; left-join the typed detail table per type to add the secondary fields (creature: `size`, `cr`; spell: `level`, `school`).
  - `get_entity_public(session, entity_id)` -> spine + `_flatten_detail(typed)` (reuse `visibility._flatten_detail`), or None -> 404.
  - `list_relationships_public(session, entity_id)` -> both directions, `{direction, rel_type, entity:{id,name,entity_type}}`, no recognition dimming.
  - `search_public(session, q)` -> reuse `search.py` name + semantic resolvers without the grant predicate; shape `{entities, lore}` with book `display_name` + section title on lore hits.
- New `grimoire/router_public.py`: `APIRouter(prefix="/api/grimoire")` with the endpoints in the contract table. Books/sections reuse `library.list_books`/`list_sections` directly. Image endpoint reuses the private streaming code (extract the shared S3-stream helper into `public.py` or import it).
- `grimoire/__init__.py`: add
  ```python
  def register_public(app: FastAPI) -> None:
      from grimoire.router_public import router as public_router
      app.include_router(public_router)
  ```
- `app/main_public.py`: `import grimoire` + `grimoire.register_public(app)`.
- Tests: `grimoire/public_test.py` (unit, SQLite `create_all` fixtures) + `grimoire/router_public_test.py` (endpoint shapes, no campaign/as needed, 404 on missing). Assert entity list secondary fields and that private tables are never queried. Hand-add both `py_test` targets in `projects/monolith/BUILD` (repo memory: new `*_test.py` need hand-added rules; prefer extending patterns already there).
- Follow the async/session rules in `projects/monolith/CLAUDE.md` (all Session I/O sync; endpoints use `Depends(get_session)`).

### Task 3 - Frontend: public route skeleton + API client + gated layout

- New `frontend/src/lib/public/grimoire/api.js`: `apiFetch(path)` -> `fetch(\`/api/grimoire${path}\`)`(public paths, no campaign/as); route href builders;`renderChunk.js` ported from the private lib (structural block parser) + its vitest.
- New `frontend/src/routes/public/app/grimoire/+layout.svelte` + `+layout.js` (`ssr=false`): `Nav`, `Seo` (with `noindex`), `TurnstileGate` gate (only render children / fetch after `onAdmitted`), `Footer`. Import `design-system.css` via the public tier's existing global (confirm how other public apps load it; likely already global). `<meta name="robots" content="noindex">`.
- Client-fetch AFTER admission so the Turnstile gate is real (no corpus in SSR HTML). Matches grimoire's existing `ssr=false` client-fetch pattern.
- Route files: `+page.svelte` (Library), `book/[book]/+page.svelte`, `book/[book]/c/[chunk]/+page.svelte`, `entities/+page.svelte`, `entity/[id]/+page.svelte`.

### Task 4 - Frontend: Library + section tree + chunk reader (design system)

- Library: `.card-hard` book rows, coverage as a chunky bordered bar, stat chips, `Marquee` corpus ticker (`<BOOK> · N CHUNKS · N IMAGES · N ENTITIES · KG SYNCED`), `Sticker`/`.hl-yellow` for NEW. `.display` headings, `.eyebrow` labels, `.btn-primary` READ.
- Section tree + chunk reader: reader uses `.wrap-narrow` (880px) measure, `renderChunk` structural rendering (bullets/headings/paragraphs), section label deduped vs first line, prev/next as `.btn` blocks, `{seq+1} / {chunk_count}` position, image chunks via the streaming endpoint, "on this page" entity chips linking to entity detail.
- Responsive: two-pane (list + reader) at >=880px is optional; simplest is single-column with back nav. Match the composition of the v5 mockup (left-clustered rows, full-height READ) but expressed in design-system classes.

### Task 5 - Frontend: entities index + stat blocks (design system)

- Entities index: `.card-hard` grid or dense list, `BrutalistSelect` (or chip row) type filter, secondary line (creature size/CR, spell level/school) from the list payload, search box feeding `/search`.
- Entity detail: dispatcher on `entity_type` -> Creature / Spell / Generic renderers. Stat block = classic content in a `.card-hard` brutalist frame: name (`.display`), size/type/alignment strap, AC/HP/speed row, ability-score grid, traits/actions prose, tapered-rule dividers. Sources (mentions -> reader) and relationships sections. Restyle the existing private `statblock/*` structure to design-system tokens; JSONB overflow renders as labeled prose, never raw JSON.

### Task 6 - Rollout: dual chart bump + values/creds

- Bump `projects/monolith/chart/Chart.yaml` `0.277.0 -> 0.278.0` and `deploy/application.yaml` targetRevision to match.
- Bump `projects/monolith-public/chart/Chart.yaml` `0.84.0 -> 0.85.0` and `deploy/application.yaml` targetRevision to match.
- Verify the `monolith-public` pod can stream image chunks: check whether it has the SeaweedFS/S3 secret the image endpoint needs; if not, mirror it (Kyverno clone pattern) or degrade gracefully (reader hides images that 500). Confirm before merge.
- Confirm the public SvelteKit build serves `/public/app/grimoire/*` with no extra values gating (the frontend is the same built app across tiers).

### Task 7 - Review, CI, merge, verify

- One comprehensive end-of-PR Opus review against this plan (repo policy).
- `format`, push, watch `gh pr checks`, fix via BuildBuddy logs.
- `gh pr merge --auto --rebase`. Poll to merged; verify both ArgoCD apps sync; click through the public route (solve Turnstile, browse a book, read a chunk, open a creature stat block).

---

## Constraints

- Reuse the public design system; **no new custom token/theme fork**, no new deps, no em-dashes anywhere.
- `ssr=false`, client-fetch after Turnstile admission; `noindex` on every grimoire public page.
- 44px min touch targets; transform/opacity-only motion with `prefers-reduced-motion` guards (design-system already respects this).
- Backend: all Session I/O sync per `projects/monolith/CLAUDE.md`; new `*_test.py` need hand-added `py_test` in `projects/monolith/BUILD`.
- No local tests: push and iterate via `gh pr checks` + `mcp__buildbuddy__*` logs.

## Risks

- **S3 creds on `monolith-public`** (Task 6) - images may 500 until the secret is mirrored; reader must degrade gracefully.
- **Turnstile on a data app** - the gate is only meaningful because we client-fetch after admission; do not add a `+page.server.js` that SSR-embeds the corpus.
- **Copyright** - noindex + Turnstile is the agreed mitigation; do not add the route to `sitemap.xml` or `robots.txt` allow.
- **Search embedding path** - `search_public` must reach the same embedding/vector path the private search uses; verify the public tier can compute/query embeddings (or restrict public search to name-only if the embed client is private-tier only).
