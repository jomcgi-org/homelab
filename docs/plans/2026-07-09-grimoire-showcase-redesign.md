# Grimoire Showcase Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task in this session. Per repo CLAUDE.md: implementers self-review before each commit, ONE comprehensive code review at end of PR, all test execution deferred to CI on the pushed branch.

**Goal:** Rebuild the public Grimoire inner pages (Library, merged World, Book reader, Chat) as one cohesive "you are inside the book" showcase, per the approved design in `2026-07-09-grimoire-showcase-redesign-design.md`.

**Architecture:** SvelteKit public routes under `projects/monolith/frontend/src/routes/public/app/grimoire/`, shared lib under `src/lib/public/grimoire/`. Small additive changes to the FastAPI public router (`projects/monolith/grimoire/`). Entities + Explore merge into a new `world` route; old routes redirect. Session constellation lifts into a shared sessionStorage-backed store.

**Tech Stack:** Svelte 5, canvas 2D (existing ExploreCanvas physics), vitest for pure JS modules, pytest for backend, Playwright visual regression (`frontend/visual/`).

**Key existing endpoints (verified):**
- `GET /api/grimoire/books` (library list), `GET /api/grimoire/books/{id}/sections` (flat sections with `section_path` like `Chapter 3/Introduction`, `first_chunk_id`), `GET /api/grimoire/books/{id}/read` (paginated chunks with `image_url`)
- `GET /api/grimoire/chunks/{chunk_id}/image` (serves S3 image; frontend proxy `/app/grimoire/api/chunks/{id}/image`)
- `GET /api/grimoire/entities?q=&type=&limit=&cursor=` (search; default order = degree desc)
- `GET /api/grimoire/explore/ego?id={entity_id}` (nodes + edges `{from, to, rel_type}`) via `exploreEgo()` in `src/lib/public/grimoire/api.js:86`
- `GET /api/grimoire/adventures` (scope list, `list_all_adventures` in `grimoire/library.py:509`)

**Repo constraints:** No local test loop (push and watch CI). No em-dashes in any copy. Chart bumps for BOTH `projects/monolith` and `projects/monolith-public` in this PR (`bazel/tools/git/bump-chart.sh`). Run `bazel/tools/format/fast-format.sh` before each commit. New public callables may trip `bdd_completeness_test`; add BDD entries as needed. New monolith `*_test.py` files need hand-added `py_test` targets (gazelle will not).

---

### Task 1: Backend, cover + entity art selection

**Files:**
- Modify: `projects/monolith/grimoire/library.py` (books listing)
- Modify: `projects/monolith/grimoire/public.py` (entity payloads)
- Modify: `projects/monolith/grimoire/explore.py` (`ego_subgraph`)
- Test: colocated `*_test.py` per existing test layout in `projects/monolith/grimoire/` (check BUILD for py_test pattern; hand-add targets)

**Step 1:** In the books listing query (feeds `GET /api/grimoire/books`), add `cover_chunk_id`: for each book, the `id` of the earliest chunk (`ORDER BY seq`) with `image_ref IS NOT NULL`, skipping the first 3 chunks when possible (front-matter scans are usually chunk 0-2; use a window/lateral select: prefer `seq >= 4`, fall back to any image chunk). NULL when the book has no images.

**Step 2:** Add `image_chunk_id` to the focused entity in `ego_subgraph` (explore.py) and to the entity detail payload in public.py: earliest chunk with `image_ref IS NOT NULL` joined via `grimoire.chunk_entity_mention` for that entity. One extra query per ego call is fine; do NOT compute art for neighbor nodes.

**Step 3:** Write pytest tests (SQLite fixtures use `create_all`, no migrations, per repo convention): book with images gets `cover_chunk_id`, book without gets NULL; entity with mention on an image chunk gets `image_chunk_id`. Hand-add `py_test` targets in the BUILD file (gazelle will not pick them up).

**Step 4:** Format, commit `feat(grimoire): expose cover_chunk_id and entity image_chunk_id`.

### Task 2: Shared theme shell, nav, and page transition

**Files:**
- Modify: `projects/monolith/frontend/src/routes/public/app/grimoire/+layout.svelte` (or the layout that renders the GRIMOIRE nav; locate it, it renders LIBRARY/ENTITIES/EXPLORE/CHAT)
- Modify: `projects/monolith/frontend/src/lib/grimoire/theme.css`
- Create: `projects/monolith/frontend/src/lib/public/grimoire/PageTurn.svelte`

**Step 1:** Nav becomes `Library / World / Chat` (World links `/app/grimoire/world`). Restyle the nav bar onto the grimoire identity: parchment-dark surface token, serif wordmark, type-colored active underline. Keep `.grimoire.dark` scoping rules (reskin gotcha: design system is site-wide, scope everything under the grimoire root class).

**Step 2:** `PageTurn.svelte`: wraps page content, on route change plays a ~300ms slide+fade (transform/opacity only, never layout). Instant cut under `prefers-reduced-motion`. Use Svelte transitions keyed on `$page.url.pathname` section segment. Wire into the grimoire layout.

**Step 3:** Remove "THE GRIMOIRE" eyebrow from Library page and any `← LIBRARY` back-links on entities/explore/chat pages (grep `eyebrow` and `← ` / `&larr;` under the grimoire routes).

**Step 4:** Format, commit `feat(grimoire): grimoire nav shell, page-turn transitions, drop redundant wayfinding`.

### Task 3: Shared session constellation store + dock

**Files:**
- Create: `projects/monolith/frontend/src/lib/public/grimoire/constellation-store.js`
- Create: `projects/monolith/frontend/src/lib/public/grimoire/constellation-store.test.js`
- Create: `projects/monolith/frontend/src/lib/public/grimoire/ConstellationDock.svelte`
- Modify: chat page to consume the store instead of local state (`routes/public/app/grimoire/chat/+page.svelte`, seeds at lines ~210-216, panel at ~418-423)

**Step 1 (test first):** vitest tests for the store: `touch(entity)` adds node once, hydrates from `sessionStorage`, serializes Sets/Maps correctly (existing `constellation-state.js` uses `Set`/`Map`; the store must (de)serialize via arrays), `withEgo` edges recompute. Literal `import { describe, it, expect } from "vitest"` per repo test style.

**Step 2:** Implement the store: thin Svelte store wrapping the existing pure functions from `chat/constellation-state.js` (move that module to `lib/public/grimoire/constellation-state.js`, update imports), persisting to `sessionStorage` key `grimoire.constellation` on change, hydrating on init (guard `typeof sessionStorage`, SSR-safe).

**Step 3:** `ConstellationDock.svelte`: collapsed pill (node count + mini preview using `MiniConstellation.svelte`), expands on hover/tap to the panel; each node links to `/app/grimoire/world?e={id}`. Render it from the grimoire layout so it appears on every page. Empty state: hidden.

**Step 4:** Chat page: replace local constellation state with the store (its `node_touched` frames call `store.touch`, ego fetches call `store.withEgo`). Keep the existing right-hand panel behavior on wide chat, backed by the same store.

**Step 5:** Format, commit `feat(grimoire): shared session constellation store and cross-page dock`.

### Task 4: Library shelf

**Files:**
- Modify: `projects/monolith/frontend/src/routes/public/app/grimoire/library/+page.svelte`

**Step 1:** Replace list rows with a shelf grid of cover cards. Cover image: `/app/grimoire/api/chunks/{cover_chunk_id}/image` when present; else a generated typographic cover (book initials + kind-tinted background, deterministic hue from book_id hash). `loading="lazy"`, fixed aspect ratio (2:3), `object-fit: cover`.

**Step 2:** Card contents: title, kind, 2 stat chips (entities, images; drop chunks from cards). Copyrighted books: muted cover (slight desaturate/opacity) + bordered chip `[lock glyph] Reference only` on the card. Readable books: full color, `Read` affordance, page-lift hover (translateY + shadow, transform-only).

**Step 3:** Totals line under the title becomes one quiet sentence; keep All/Readable toggle; keep kind grouping as shelf sections.

**Step 4:** Format, commit `feat(grimoire): library shelf with covers, stat chips, labeled reference badge`.

### Task 5: World page (merge Entities + Explore)

The big one. Reuse `ExploreCanvas.svelte`, `ExploreCodex.svelte`, `api.js` wrappers.

**Files:**
- Create: `routes/public/app/grimoire/world/+page.svelte` (+ `+page.server.js` if the explore page has one; mirror its data loading)
- Create: `src/lib/public/grimoire/world/EntitySearch.svelte` (typeahead)
- Create: `src/lib/public/grimoire/world/ScopePicker.svelte` (styled combobox)
- Create: `src/lib/public/grimoire/world/relationship-phrases.js` + `.test.js`
- Modify: `src/lib/public/grimoire/explore/ExploreCodex.svelte` (card redesign)
- Modify: `routes/public/app/grimoire/entities/+page.svelte`, `explore/+page.svelte` → replace with redirects to `/app/grimoire/world` (SvelteKit `redirect(301, ...)` in `+page.server.js`, preserve `?e=` where an entity was focused)

**Step 1 (test first):** `relationship-phrases.test.js`: given focus id and edge `{from, to, rel_type}` plus peer name, outgoing `owns` → `{ pre: "owns ", peer, post: "" }`; incoming → `{ pre: "", peer, post: " owns this" }`; symmetric types (`ally_of`, `related_to`, `associated_with`, plus any found by `grep -o 'rel_type[^,]*'` sampling; keep a `SYMMETRIC` set) → `{ pre: "ally of ", peer, post: "" }` regardless of direction. Humanize rel_type (`snake_case` → spaced).

**Step 2:** Implement `relationship-phrases.js` (pure function, no DOM).

**Step 3:** World page layout: header row = EntitySearch (debounced `GET /api/grimoire/entities?q=`, results grouped by type with `--grim-type-*` dots, keyboard nav) + ScopePicker (custom listbox: filter input, `Everything` first, adventures grouped under book display names; ARIA combobox pattern; replaces native select) + existing lens tabs restyled. Below: full-viewport flex canvas region (fixes bottom cutoff: `min-height: 0`, canvas fills `flex: 1` container, resize observer) + docked codex.

**Step 4:** Graph behavior: selecting an entity (search, node click, `?e=` param, dock click) loads `exploreEgo(id)` and renders the ego neighborhood centered on it, camera auto-fit with existing easing; scope + lens filter the ego's nodes when set. Node click re-centers (fetch new ego, keep shared nodes in place, reuse the settle-then-fade pattern already in ExploreCanvas). Report each focused entity to the constellation store.

**Step 5:** Landing state: `FEATURED = ["Strahd von Zarovich", "Tiamat", "Waterdeep", "Zariel", "Acererak"]`; pick by day-of-year modulo, resolve via `GET /api/grimoire/entities?q={name}&limit=1`, open card + ego. Fallback: first result of degree-ordered `GET /api/grimoire/entities?limit=1`.

**Step 6:** Codex card redesign in `ExploreCodex.svelte`: compact header (type badge, name at reasonable scale, entity art via `image_chunk_id` from Task 1 else type-colored monogram device), description/stats, relationships as phrases from Step 2 grouped under quiet rel_type subheads, "appears in" excerpts linking to reader chunks (existing `chunkHref`). Bottom sheet under 900px.

**Step 7:** Redirects for `/entities` and `/explore`; move the type-count chips (from old entities page) into the EntitySearch dropdown as filter chips.

**Step 8:** Format, commit `feat(grimoire): world page merging entities and explore, ego-first graph, directed phrases`.

### Task 6: Book reader

**Files:**
- Modify: `routes/public/app/grimoire/book/[book]/+page.svelte` (+ server load)
- Create: `src/lib/public/grimoire/book/section-tree.js` + `.test.js`

**Step 1 (test first):** `section-tree.test.js`: builds a tree from the flat `/sections` list by splitting `section_path` on `/` (two-level: chapter → section); chapters keep reading order; sections with no `/` are top-level leaves; duplicate chapter names merge.

**Step 2:** Implement `section-tree.js`; TOC renders the tree collapsible (chapter rows toggle; current section's chapter auto-expanded, others collapsed; current row highlighted). Remove chunk-count numbers from rows.

**Step 3:** Layout: kill the sub-nav strip (book title + `DRAGONBORN 30/5631` bar); book title heads the TOC column; add a 2px reading-progress bar fixed under the main nav (scroll fraction). Widen content column to ~72ch, center sidebar+content as one grid, render chunk images inline at column width (`image_url` already in `/read` response).

**Step 4:** Entity mentions in chunk text: reuse `mention-highlight.js` against the book's entity mentions if the read payload carries them; if it does not, skip linkification in this PR and note it in the plan review (do NOT add a new heavy endpoint here; YAGNI). Clicking a highlighted mention opens the World codex as an overlay (import ExploreCodex, fetch ego detail) without navigation.

**Step 5:** Format, commit `feat(grimoire): nested TOC, full-width reader, inline art, remove sub-nav`.

### Task 7: Chat surfaces + visible highlighting

**Files:**
- Modify: `src/lib/public/grimoire/chat/mention-highlight.js` (+ its test)
- Modify: `routes/public/app/grimoire/chat/+page.svelte` (+ session route `chat/s/[id]/+page@.svelte` if it duplicates styles)

**Step 1:** `mention-highlight.js`: emitted span becomes `class="gmark" style="color: var(--grim-type-${type}, currentColor)"` plus a `data-type` attr; CSS gives `.gmark` a soft tinted background via `color-mix(in srgb, currentColor 12%, transparent)`, subtle underline retained. Update the existing test's expected markup. Keep the TYPE_ALLOWLIST validation exactly as is.

**Step 2:** Make `.gmark` spans clickable links to `/app/grimoire/world?e={entity_id}` (highlightMentions already receives touched items with ids; emit `<a>` not `<span>`, keep single-pass contract).

**Step 3:** Message bodies get white card surfaces (rounded, subtle border/shadow) on the parchment page background; user bubbles stay distinct. Grounding chips ("GROUNDED IN") become links to World for entity-kind touches.

**Step 4:** Replace the in-page constellation panel wiring with the shared store/dock from Task 3 (wide-screen chat keeps the large panel, reading from the store).

**Step 5:** Format, commit `feat(grimoire): visible entity highlighting, white message surfaces, linked grounding`.

### Task 8: Visual regression + BDD + chart bumps

**Files:**
- Modify: `projects/monolith/frontend/visual/targets.json` (replace `grimoire-entities` with `grimoire-world`; confirm `grimoire-library`, `grimoire-book` still match new DOM; add `grimoire-chat` if mockable)
- Modify: `projects/monolith/frontend/visual/mock-server.mjs` fixtures for new fields (`cover_chunk_id`, `image_chunk_id`, ego for featured entity)
- BDD: if `bdd_completeness_test` trips on new public backend callables, add the missing entries per repo convention
- Chart bumps: `bazel/tools/git/bump-chart.sh projects/monolith` AND `bazel/tools/git/bump-chart.sh projects/monolith-public`

**Steps:** Update fixtures/targets, run format, commit `chore(grimoire): visual targets, fixtures, chart bumps`. Push branch, open PR, `gh pr checks --watch`, fix CI failures by reading BuildBuddy logs (quote errors verbatim). One comprehensive end-of-PR code review (Opus reviewer) before merge. Verify live after ArgoCD sync: load `/app/grimoire/world` and the library shelf on jomcgi.dev, confirm covers render (SSR content signal, not ArgoCD version).

---

## Execution notes

- Tasks 1-3 are foundations; 4-7 can proceed after their dependencies (4 needs 1+2; 5 needs 1+2+3; 6 needs 2; 7 needs 2+3). Run implementers sequentially in the single worktree (no parallel committing subagents in one worktree) unless using isolation worktrees.
- All copy: no em-dashes anywhere.
- SSR: any new npm dep must go in `ssr.noExternal` (prod 500s otherwise). Prefer zero new deps; everything above is achievable with Svelte + existing canvas code.
