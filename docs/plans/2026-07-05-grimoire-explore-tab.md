# Grimoire EXPLORE Tab Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship a public, delightful "EXPLORE" experience for the grimoire corpus: one canvas with swappable lenses (World, Story, Quests, Rules) over the whole extracted D&D universe, scoped by Adventure, entered by gallery / search / serendipity, and looped back into the source books.

**Architecture:** A new public-tier SvelteKit route (`/app/grimoire/explore`) renders a hand-rolled Canvas graph of entities (nodes) and typed relationships (edges), backed by new corpus-global, grant-free read endpoints on `router_public.py`. Scope and lens are two orthogonal dials computed as SQL union predicates over the existing `(category, temporality, entity_type)` columns plus the `adventure` / `adventure_entity` layer. No new extraction and (except an optional precomputed-similarity table) no schema change: every surface is a query over data that already exists.

**Tech stack:** Python 3 / FastAPI / SQLModel / pgvector (backend), SvelteKit + Canvas 2D + `@dagrejs/dagre` (already a frontend dep) for the Story/Rules DAG layouts, Bazel + BuildBuddy CI, apko images, ArgoCD GitOps.

---

## Design decisions (read before starting)

1. **Tier: PUBLIC.** The goal is "delight people and let them explore the world, separate from playing." That is the ungated public tier (`main_public.py` / `register_public`, `monolith-public` chart, `public_reader` DB role). Consequence: **no request-time embedding** on the public network, so free-text "vibe search" is out of scope for v1 (name/content search only, via the existing `/search`), and "more like this" is served from a **precomputed** neighbor table (Phase D). This is the one load-bearing decision; if Joe wants EXPLORE private instead, the vector-search tasks change but nothing else does. **Before touching public code, read `docs/runbooks/public-tier-checklist.md`.**

2. **Scope x Lens are orthogonal.**
   - **Scope** = which slice: `everything` | `adventure:{id}` | `book:{id}` | `search:{q}`. Default landing = the **Adventure gallery**.
   - **Lens** = how to view the slice: `world` | `story` | `quests` | `rules`.
   - Lens membership is a **union predicate**, never a stored flag:
     - `world`  = `category='lore'` OR (`entity_type IN ('event','quest')` AND `temporality='historical'`)
     - `story`  = `entity_type='event'` (all categories), ordered by temporality + `PRECEDED`/`CAUSED`
     - `quests` = `entity_type='quest'`, split resolved (historical) vs active (present/future)
     - `rules`  = `category='mechanics'` OR `entity_type='spell'`

3. **Visual direction: reskin grimoire TO the demo palette (not the reverse).** Joe's call (2026-07-05): the oxblood/parchment theme reads poorly; adopt the demo's clean, readable palette across the whole grimoire app. We retheme the shared CSS custom properties in `projects/monolith/frontend/src/lib/grimoire/theme.css` to the demo values (cool paper `#f3f5f7`, ink `#1a1f28`, indigo accent `#33507a`, plus the 7+ entity-type jewel hues and relationship-family hues promoted to tokens). Components already read these tokens, so the new look cascades to Library, Entities, EntityDetail/statblocks, the Reader, and EXPLORE from one edit. This ships as its OWN PR ahead of EXPLORE (see Phase B0), reviewed via the repo's automatic before/after visual-regression images. The mockup at `docs/plans/assets/2026-07-05-grimoire-explore-mockup.html` is BOTH the interaction and the visual spec for EXPLORE; the ExploreCanvas draws node/edge colors from the shared tokens, never hardcoded hex. The renderer itself reuses the mockup's hand-rolled Canvas force/draw/pan/zoom/hit-test code ported to a Svelte component; after Phase B0 the grimoire tokens ARE the demo palette, so reading `--grim-*` tokens gives the correct visual automatically.

4. **Bulk over N+1.** The client must not call `/entities/{id}/relationships` once per node. Add one **subgraph endpoint** that returns `{nodes, edges}` for a scope in a single payload, plus a 1-hop **ego** expansion for wandering.

5. **Reuse, do not rebuild.** Reuse `EntityDetail.svelte` + `statblock/*` for the codex body, `theme.css` tokens, `api.js` fetch helpers, and the public `+layout.svelte` gate. New code is the canvas, the scope/lens chrome, and the endpoints.

6. **Ship needs BOTH chart bumps.** A public-tier change rebuilds `image_public` and deploys via the `monolith-public` chart. Per repo lore, apex/public rollouts need both `projects/monolith` AND `projects/monolith-public` chart bumps. Phase E does both.

**Key files (verified in worktree):**
- Backend router (public): `projects/monolith/grimoire/router_public.py` (prefix `/api/grimoire`, mounted via `grimoire.register_public` in `projects/monolith/app/main_public.py:54`)
- Backend corpus reads: `projects/monolith/grimoire/public.py`, `library.py`, `search.py`, `visibility.py`
- Models: `projects/monolith/grimoire/models.py` (Entity spine has `category` STORED gen col, `temporality`, `detail` JSONB; `Adventure`; `adventure_entity` VIEW; Relationship has nullable source-chunk provenance)
- Test pattern: `projects/monolith/grimoire/router_public_test.py` (SQLite `create_all`, schema-strip fixture)
- Py test registration: hand-added `py_test` in `projects/monolith/BUILD` (gazelle excludes grimoire) - copy an existing `grimoire_*_test` stanza
- Frontend public routes: `projects/monolith/frontend/src/routes/public/app/grimoire/`
- Public nav: `projects/monolith/frontend/src/routes/public/app/grimoire/+layout.svelte`
- Reusable components + fetch: `projects/monolith/frontend/src/lib/grimoire/` (`EntityDetail.svelte`, `statblock/*`, `api.js`, `theme.css`)
- Public grants: `projects/monolith/chart/migrations/20260704000000_grimoire_public_reader_grant.sql` (+ adventure grant in `20260705160000_grimoire_adventure.sql`)

---

## Phase A: Backend corpus-graph API

All new endpoints go on `router_public.py` (grant-free, `is_global` corpus). Add pure query logic to `library.py` / a new `explore.py` module so the SQLite `create_all` fixtures can drive it directly; keep the route thin. Each new `*_test.py` needs a hand-added `py_test` stanza (Phase E, Task E1 lists them, but write the test file in the same task as its code and register it immediately).

### Task A1: List-all-adventures endpoint (gallery data)

**Files:**
- Modify: `projects/monolith/grimoire/library.py` (add `list_all_adventures`)
- Modify: `projects/monolith/grimoire/router_public.py` (add `GET /adventures`)
- Test: `projects/monolith/grimoire/router_public_test.py` (extend)

**Step 1: Write the failing test.** Append to `router_public_test.py`:

```python
def test_list_all_adventures_across_books(client, session):
    # seed two books, each with one adventure and one in-range chunk+entity
    _seed_adventure(session, book_id="cos", name="Curse of Strahd", seq=0,
                    start_seq=0, end_seq=100, entity_name="Strahd", level_range="1-10")
    _seed_adventure(session, book_id="lmop", name="Lost Mine of Phandelver", seq=0,
                    start_seq=0, end_seq=50, entity_name="Gundren", level_range="1-5")
    body = client.get("/api/grimoire/adventures").json()
    names = {a["name"] for a in body}
    assert names == {"Curse of Strahd", "Lost Mine of Phandelver"}
    cos = next(a for a in body if a["name"] == "Curse of Strahd")
    assert cos["book_id"] == "cos"
    assert cos["book_display_name"]  # joined from book table
    assert cos["level_range"] == "1-10"
    assert cos["entity_count"] == 1
```

Add a `_seed_adventure` helper near the top of the test module (mirror the existing adventure seeding in `router_public_test.py` around lines 170-195; create `Book`, `Adventure`, a `KnowledgeChunk` with `seq` in range, an `Entity(is_global=True)`, and a `ChunkEntityMention`).

**Step 2: Run to verify it fails.** `bazel` is not for local; instead confirm the assertion shape by reading `library.list_adventures` (already returns `entity_count`). Expected failure: `404` / attribute error, endpoint missing.

**Step 3: Implement.** In `library.py`, add (mirror `list_adventures` but no `book_id` filter, join `Book.display_name`, order by book then seq):

```python
def list_all_adventures(session: Session) -> list[dict[str, Any]]:
    """Every adventure across all books, seq-ordered within book, each with
    entity_count and its book's display_name. Powers the EXPLORE gallery."""
    counts = dict(
        session.execute(
            _adventure_chunk_join(
                select(
                    Adventure.id,
                    func.count(func.distinct(ChunkEntityMention.entity_id)),
                )
            ).group_by(Adventure.id)
        ).all()
    )
    rows = session.exec(
        select(Adventure, Book.display_name)
        .join(Book, Book.id == Adventure.book_id)
        .order_by(Book.display_name, Adventure.seq)
    ).all()
    return [
        {
            "id": str(a.id),
            "book_id": a.book_id,
            "book_display_name": display_name,
            "name": a.name,
            "summary": a.summary,
            "level_range": a.level_range,
            "seq": a.seq,
            "entity_count": int(counts.get(a.id, 0)),
        }
        for a, display_name in rows
    ]
```

In `router_public.py`, add above the per-book route:

```python
@router.get("/adventures")
def list_adventures_all(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """All adventures across the corpus, for the EXPLORE gallery. See
    library.list_all_adventures."""
    return library.list_all_adventures(session)
```

**Step 4: Verify shape** by re-reading the test; ensure `_adventure_chunk_join` import exists in `library.py`.

**Step 5: Commit.** `feat(grimoire): list-all-adventures public endpoint for EXPLORE gallery`

### Task A2: Lens/scope filter helper

**Files:**
- Create: `projects/monolith/grimoire/explore.py`
- Test: `projects/monolith/grimoire/explore_test.py`

**Step 1: Failing test** (`explore_test.py`) - drive the predicate builder directly:

```python
from sqlmodel import select
from grimoire.explore import lens_predicate
from grimoire.models import Entity

def _types(session):
    return {e.entity_type for e in session.exec(select(Entity).where(lens_predicate("world"))).all()}

def test_world_lens_includes_lore_and_historical_events(session):
    _seed_entity(session, "npc", "Strahd")
    _seed_entity(session, "event", "Fall of Barovia", temporality="historical")
    _seed_entity(session, "event", "The Ceremony", temporality="future")
    _seed_entity(session, "class", "Wizard")
    assert _types(session) == {"npc", "event"}  # future event + mechanics excluded

def test_rules_lens_unions_spell(session):
    _seed_entity(session, "class", "Wizard")
    _seed_entity(session, "spell", "Fireball")
    _seed_entity(session, "npc", "Strahd")
    got = {e.name for e in session.exec(select(Entity).where(lens_predicate("rules"))).all()}
    assert got == {"Wizard", "Fireball"}
```

`_seed_entity` inserts an `Entity(is_global=True, entity_type=..., temporality=...)` and commits (category derives via the Computed column in SQLite create_all).

**Step 3: Implement** `explore.py`:

```python
"""EXPLORE lens/scope query predicates. Lens membership is a union over
(category, temporality, entity_type), never a stored flag (see plan design
decision 2). category is a STORED generated column, so these are cheap WHERE
clauses."""
from __future__ import annotations
from sqlalchemy import or_
from sqlmodel import and_
from grimoire.models import Entity

def lens_predicate(lens: str):
    if lens == "world":
        return or_(
            Entity.category == "lore",
            and_(Entity.entity_type.in_(("event", "quest")),
                 Entity.temporality == "historical"),
        )
    if lens == "story":
        return Entity.entity_type == "event"
    if lens == "quests":
        return Entity.entity_type == "quest"
    if lens == "rules":
        return or_(Entity.category == "mechanics", Entity.entity_type == "spell")
    # "everything" / unknown -> no constraint
    return Entity.id == Entity.id
```

**Step 5: Commit.** `feat(grimoire): EXPLORE lens union predicates`

### Task A3: Subgraph endpoint (bulk nodes+edges for a scope)

The core performance surface. Returns every node in the scope+lens and every edge whose *both* endpoints are in that node set.

**Files:**
- Modify: `projects/monolith/grimoire/explore.py` (add `scope_subgraph`)
- Modify: `projects/monolith/grimoire/router_public.py` (add `GET /explore/graph`)
- Test: `projects/monolith/grimoire/explore_test.py`

**Step 1: Failing test** (router-level, via `client`):

```python
def test_subgraph_adventure_scope_world_lens(client, session):
    adv = _seed_adventure(session, book_id="cos", name="Curse of Strahd",
                          seq=0, start_seq=0, end_seq=100, entity_name="Strahd")
    strahd = _entity_id(session, "Strahd")
    barovia = _seed_entity(session, "location", "Barovia", book_id="cos", seq=1)  # in range
    _seed_relationship(session, strahd, barovia, "LOCATED_IN")
    _seed_entity(session, "class", "Wizard", book_id="cos", seq=2)  # in range but rules-only
    body = client.get(f"/api/grimoire/explore/graph?scope=adventure:{adv}&lens=world").json()
    node_names = {n["name"] for n in body["nodes"]}
    assert node_names == {"Strahd", "Barovia"}   # Wizard excluded by world lens
    assert body["edges"] == [{"from": strahd, "to": barovia, "rel_type": "LOCATED_IN"}]
    # node carries spine + secondary detail for codex-lite
    assert next(n for n in body["nodes"] if n["name"] == "Barovia")["entity_type"] == "location"
```

**Step 3: Implement.** In `explore.py`:

```python
def scope_entity_ids(session, scope: str, lens: str) -> set[str]:
    """Resolve the node set for a (scope, lens). scope is 'everything',
    'adventure:{id}', or 'book:{id}'. Applies is_global + lens predicate."""
    from grimoire.models import Adventure, ChunkEntityMention, KnowledgeChunk
    q = select(Entity.id).where(Entity.is_global, lens_predicate(lens))
    if scope.startswith("adventure:"):
        adv_id = scope.split(":", 1)[1]
        adv = session.get(Adventure, adv_id)
        if adv is None:
            return set()
        roster = select(ChunkEntityMention.entity_id).join(
            KnowledgeChunk, KnowledgeChunk.id == ChunkEntityMention.chunk_id
        ).where(
            KnowledgeChunk.book_id == adv.book_id,
            KnowledgeChunk.seq >= adv.start_seq,
            or_(adv.end_seq is None, KnowledgeChunk.seq <= (adv.end_seq or 1 << 31)),
        )
        q = q.where(Entity.id.in_(roster))
    elif scope.startswith("book:"):
        book_id = scope.split(":", 1)[1]
        roster = select(ChunkEntityMention.entity_id).join(
            KnowledgeChunk, KnowledgeChunk.id == ChunkEntityMention.chunk_id
        ).where(KnowledgeChunk.book_id == book_id)
        q = q.where(Entity.id.in_(roster))
    return {r for r in session.exec(q).all()}

def scope_subgraph(session, scope: str, lens: str) -> dict:
    """{nodes, edges} for a scope+lens. Edges are relationships whose BOTH
    endpoints are in the node set (induced subgraph)."""
    from grimoire.models import Relationship
    ids = scope_entity_ids(session, scope, lens)
    if not ids:
        return {"nodes": [], "edges": []}
    nodes = [
        _node_projection(e)  # spine + secondary detail, reuse public._entity_secondary
        for e in session.exec(select(Entity).where(Entity.id.in_(ids))).all()
    ]
    edges = [
        {"from": r.from_entity_id, "to": r.to_entity_id, "rel_type": r.rel_type}
        for r in session.exec(
            select(Relationship).where(
                Relationship.from_entity_id.in_(ids),
                Relationship.to_entity_id.in_(ids),
            )
        ).all()
    ]
    return {"nodes": nodes, "edges": edges}
```

`_node_projection` returns `{id, entity_type, name, category, temporality, ...secondary}`; reuse the secondary-field flattening already in `public.py` (creature size/cr, spell level/school, location region). Factor a small `public._entity_card(entity)` if one does not already exist and import it.

Route:

```python
@router.get("/explore/graph")
def explore_graph(scope: str = "everything", lens: str = "world",
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    """Induced subgraph {nodes, edges} for a scope + lens. See explore.scope_subgraph."""
    return explore.scope_subgraph(session, scope, lens)
```

**Guardrail (log, do not silently cap):** if `len(nodes) > 1500`, still return them but include `"truncated": false` today; add a `warning` field only when a real cap is introduced. Note in the response docstring that whole-corpus `everything` scope may be large and the frontend should default to gallery/adventure scope.

**Step 5: Commit.** `feat(grimoire): EXPLORE subgraph endpoint (induced nodes+edges per scope/lens)`

### Task A4: Ego expansion endpoint (wander from a node)

**Files:** Modify `explore.py` (`ego_subgraph`), `router_public.py` (`GET /explore/ego`), test in `explore_test.py`.

Return the focus entity + its 1-hop neighbors (grant-free, `is_global` neighbors only, mirroring `public.list_relationships_public`) as the same `{nodes, edges}` shape, so the canvas can merge it on click-to-expand. Edges include `rel_type` and the neighbor node projection. Test: seed a focus with 2 neighbors (one private/`is_global=false` that must be dropped), assert the private neighbor and its edge are absent.

**Commit.** `feat(grimoire): EXPLORE 1-hop ego expansion endpoint`

### Task A5: Pathfinding endpoint ("six degrees")

**Files:** Modify `explore.py` (`shortest_path`), `router_public.py` (`GET /explore/path?from=&to=`), test.

BFS over the `relationship` table (both directions, `is_global` nodes only), bounded to depth 6, returns the ordered node+edge chain or `{path: []}` if none. Test a 2-hop path (A LOCATED_IN B, C SERVES... ) and a no-path case. Keep it in Python (edge counts per node are small); note the bound in the docstring so it never runs away on a hairball.

**Commit.** `feat(grimoire): EXPLORE shortest-path endpoint`

---

## Phase B0: Grimoire design-system reskin (ships as its OWN PR, before EXPLORE)

Mechanical token retheme so the whole grimoire app adopts the demo's clean, readable palette. Verified by the visual-regression action (before/after/diff images posted inline on the PR for every changed grimoire page); there are no unit tests for CSS. **Land and merge this PR first, then rebase `feat/grimoire-explore` onto the new main** so EXPLORE inherits the palette.

**Dark mode (OPEN, confirm with Joe):** default is KEEP dark mode, remapped to the demo's dark variant (ink `#0d1017` + indigo + jewel, the first mockup's star-chart colors). If Joe prefers light-only for simplicity, delete the `body.dark .grimoire` overrides instead.

### Task B0.1: Retheme the light `--grim-*` tokens
Modify `projects/monolith/frontend/src/lib/grimoire/theme.css`: set `--grim-accent`/`--grim-accent-strong` to indigo (`#33507a`/`#26406a`), `--grim-paper`/`--grim-ink`/`--grim-ink-soft`/`--grim-paper-line` to the demo's cool paper + ink, keep `--grim-serif`. Grep grimoire components for any hardcoded colors and promote them to tokens. Commit: `style(grimoire): retheme light palette to the clean demo tokens`.

### Task B0.2: Entity-type + relationship-family color tokens
Add `--grim-type-{location,creature,npc,faction,deity,item,spell}` and `--grim-rel-{spatial,social,kinship,religion,creation,magic,possession,taxonomy,events,quest,mechanics,related}` custom properties to `theme.css` (values from the light mockup, extended for the v4 gameplay/mechanics types). Update the statblocks / entity list to reference these tokens instead of any inline hex, so the DOM and the EXPLORE canvas share one source of truth. Commit: `style(grimoire): entity-type and relationship-family color tokens`.

### Task B0.3: Retire the brutalist reader palette
`Reader.svelte` + `ChaptersNav.svelte` use the separate `--grimb-*` brutalist tokens (cream, `#ffde01` yellow, 2px hard borders, `Instrument Serif`, `JetBrains Mono`). Reading is the readability-critical surface (Joe's stated pain point), so remap these to the clean system: cool-paper reading surface, serif body at a comfortable ~65ch measure, indigo accents, hairline rules instead of 2px borders, drop the acid yellow. Keep the reader's structure; change only tokens/border treatments. Commit: `style(grimoire): reader adopts the clean reading palette`.

### Task B0.4: Dark-mode variant (or removal)
Per the dark-mode decision above, either remap `body.dark .grimoire` (and any `--grimb-*` dark overrides) to the demo's dark star-chart palette, or remove the dark overrides for a single light theme. Commit: `style(grimoire): dark mode matches the demo dark variant` (or `style(grimoire): single clean light theme`).

### Task B0.5: Ship the reskin PR
`bazel/tools/format/fast-format.sh`; bump BOTH charts (`bazel/tools/git/bump-chart.sh projects/monolith` and `projects/monolith-public`); push; open PR; review the visual-regression before/after for every grimoire page; merge on green. Then `git rebase origin/main` the EXPLORE branch.

---

## Phase B: Frontend EXPLORE scaffold

Public route + tab + canvas + codex, wired to Phase A. Reuse `api.js`, `theme.css`, `EntityDetail.svelte`, `statblock/*`.

### Task B1: Route + tab nav

**Files:**
- Create: `projects/monolith/frontend/src/routes/public/app/grimoire/explore/+page.svelte`
- Modify: `projects/monolith/frontend/src/routes/public/app/grimoire/+layout.svelte` (add EXPLORE crumb)
- Modify: `projects/monolith/frontend/src/lib/grimoire/api.js` (add `exploreHref`, explore fetchers)

SvelteKit is filesystem-routed (BUILD glob already captures new files, no target edit). Add a nav crumb "Explore" next to Library/Entities in the public `+layout.svelte` (mirror the existing crumb markup + `--grim-accent` active underline). Add fetch helpers to `api.js`:

```javascript
export const exploreGraph = (scope, lens) =>
  apiFetch(`/explore/graph?scope=${encodeURIComponent(scope)}&lens=${lens}`);
export const exploreEgo = (id) => apiFetch(`/explore/ego?id=${encodeURIComponent(id)}`);
export const listAllAdventures = () => apiFetch(`/adventures`);
export const explorePath = (from, to) =>
  apiFetch(`/explore/path?from=${from}&to=${to}`);
```

`+page.svelte` renders `ssr=false` (match sibling pages), a stub "EXPLORE" heading and an empty `<ExploreCanvas />` mount. Commit: `feat(grimoire): public EXPLORE route + tab`.

### Task B2: ExploreCanvas component (port the mockup renderer)

**Files:**
- Create: `projects/monolith/frontend/src/lib/grimoire/explore/ExploreCanvas.svelte`

Port the force-sim + draw + pan/zoom/hit-test from `docs/plans/assets/2026-07-05-grimoire-explore-mockup.html` into a Svelte component. Contract:
- Props: `nodes` (from the subgraph endpoint), `edges`, `focusId`, callbacks `onselect(id)`, `onexpand(id)`.
- Re-skin: node color by `entity_type` (extend the 7 lore colors with gameplay/mechanics hues), edge color by `rel_type` family (extend the family map with the v4 rels: OCCURRED_AT/INVOLVED/PRECEDED/CAUSED -> "events"; GIVEN_BY/OBJECTIVE_AT/REWARDS/ADVANCES -> "quest"; SUBCLASS_OF/FEATURE_OF/REQUIRES -> "mechanics"). Chrome and node/edge colors read the shared `--grim-*` / `--grim-type-*` / `--grim-rel-*` tokens from `theme.css` (which Phase B0 set to the demo palette), so EXPLORE matches the reskinned app automatically; the focus ring is `--grim-accent` (indigo after B0).
- Respect `prefers-reduced-motion` (freeze sim after settle, drop the twinkle) exactly as the mockup does.

Keep this a pure presentational component (no fetching). Commit: `feat(grimoire): ExploreCanvas graph renderer`.

### Task B3: Codex panel (reuse EntityDetail)

**Files:**
- Create: `projects/monolith/frontend/src/lib/grimoire/explore/ExploreCodex.svelte`

On node select, fetch `GET /entities/{id}` (full spine + typed detail) and render the body with the existing `EntityDetail.svelte` (which dispatches to `statblock/Creature|Spell|Generic`). Below it, render relationships grouped by family from the ego call, and "Appears in the books" from `GET /entities/{id}/mentions`, each mention linking to the public reader (`/app/grimoire/book/{book}/c/{chunk}`). Clicking a relationship calls `onselect(peerId)`; clicking the node's "expand" calls `onexpand(id)` which merges the ego subgraph into the canvas. Commit: `feat(grimoire): EXPLORE codex reusing EntityDetail + book mentions`.

### Task B4: Wire the page (scope=everything default for now)

Wire `+page.svelte`: on mount, `exploreGraph("everything","world")` -> feed `ExploreCanvas`; selection opens `ExploreCodex`; expand merges ego results. Add a status line (node/edge counts). Deep-link state in the URL query (`?scope=&lens=&focus=`) so views are shareable (a core delight requirement). Commit: `feat(grimoire): wire EXPLORE page (canvas + codex + deep-link state)`.

---

## Phase C: Lenses, scope selector, adventure gallery

### Task C1: Lens switcher
A segmented control (World / Story / Quests / Rules) that re-fetches `exploreGraph(scope, lens)`. For `story` and `rules`, swap the force layout for a **dagre** layered layout (`@dagrejs/dagre` is already a dep): Story lays events left-to-right by temporality + `PRECEDED`/`CAUSED`; Rules lays `class -> subclass -> class_feature` top-down via `SUBCLASS_OF`/`FEATURE_OF`. Add a `layout` prop to `ExploreCanvas` (`force` | `dagre-lr` | `dagre-tb`). Commit per lens or as one task with a test-by-eye note (visual-regression covers render).

### Task C2: Scope selector + Adventure gallery landing
- Create `projects/monolith/frontend/src/lib/grimoire/explore/AdventureGallery.svelte`: fetch `listAllAdventures()`, render cover cards (name, book_display_name, level_range, entity_count). Clicking a card sets `scope=adventure:{id}` and enters the canvas.
- EXPLORE opens on the gallery (design decision: gallery-first); "Explore everything" and search are always one click away in the chrome.
- Scope selector chip in the canvas chrome lets you switch scope without leaving.
Commit: `feat(grimoire): EXPLORE adventure gallery landing + scope selector`.

### Task C3: Search + faceted rail
- Search box wired to the existing public `GET /search` (name/content), results jump to a node (set `scope=search:{q}` returning matched entities as the node set, or focus a single entity within current scope).
- Left rail: legend (toggle entity types), category filter, book/adventure filter, a "surprise me" button (focus a random high-degree node from the current subgraph). Reuse the mockup's legend/rail markup, re-skinned.
Commit: `feat(grimoire): EXPLORE search + faceted rail`.

---

## Phase D: Delight (pathfinding, more-like-this, lore cards)

### Task D1: Six-degrees pathfinding UI
"How is X connected to Y?" picker -> `explorePath(from,to)` -> highlight the chain on the canvas as a narrative ("X SERVES the Dark Powers, who shape Barovia, where Y dwells"). Commit: `feat(grimoire): EXPLORE pathfinding UI`.

### Task D2: Precomputed "more like this" (public-safe similarity)
Public tier cannot embed at request time, so precompute. 
- Migration: `CREATE TABLE grimoire.entity_similar (entity_id uuid, similar_id uuid, rank int, score real, PRIMARY KEY (entity_id, rank))` + `GRANT SELECT ... TO public_reader` (opt-in, mirror `20260704000000`).
- Offline job (register in the grimoire jobs module, mirror `jobs.py`): for each entity embedding, `knn_embeddings` top-8 neighbors, upsert rows. Runs on the extraction-drain cadence.
- Endpoint `GET /entities/{id}/similar` reading the table; a "More like this" strip in the codex.
This is the delightful embedding-powered discovery avenue, made public-safe. Commit: `feat(grimoire): precomputed entity-similarity + more-like-this`.

### Task D3: Shareable lore cards
A per-entity card view (`/app/grimoire/explore/card/{id}`) styled as a tarokka-esque card (thematic to Curse of Strahd), with an OG-image-friendly static render for sharing. Adds to `visual/targets.json`. Commit: `feat(grimoire): shareable lore cards`.

---

## Phase E: Ship

### Task E1: Register py_test targets
Add to `projects/monolith/BUILD` (copy an existing `grimoire_*_test` stanza) for each new test file: `grimoire_explore_test`. Extend `grimoire_router_public_test` deps if needed. Verify the stanza matches the existing pattern (`imports=["."]`, `deps` include `:monolith_backend`, `@pip//fastapi`, `@pip//httpx`, `@pip//pytest`, `@pip//sqlmodel`, `@pip//pgvector`).

### Task E2: Visual regression
Add the EXPLORE page (and lore card) to `projects/monolith/frontend/visual/targets.json`:
```json
{ "id": "grimoire-explore", "path": "/app/grimoire/explore", "static_initial": true }
```
Add any new `/api/grimoire/...` fixtures to `mock-server.mjs` + `fixtures/api/` so the page renders deterministically in CI (subgraph, adventures, entity detail).

### Task E3: Migration + grants sanity
Only Phase D2 adds a table. Confirm `entity`, `relationship`, `chunk_entity_mention`, `embedding`, `adventure`, `adventure_entity` are already granted to `public_reader` (they are). Regenerate `atlas.sum` with the CI-pinned Atlas if a migration was added.

### Task E4: Chart bumps (BOTH) + PR
- `bazel/tools/git/bump-chart.sh projects/monolith`
- `bazel/tools/git/bump-chart.sh projects/monolith-public`
- Run `bazel/tools/format/fast-format.sh`.
- Push branch, open PR, watch `gh pr checks <n> --watch`, read failures via `mcp__buildbuddy__*`.
- After merge, verify live at `jomcgi.dev/app/grimoire/explore` and confirm the public rollout via ArgoCD.

### Task E5: End-of-PR review
One comprehensive Opus code review against the full diff (per repo cadence: one review per merged PR, not per task).

---

## Testing notes
- No local `bazel test` (no darwin runners): implement, commit, push, watch CI.
- SQLite fixtures use `create_all` and strip Postgres `schema=`; datetimes come back naive (assert `isinstance(..., datetime)`).
- The `category` STORED generated column derives under SQLite `create_all` via the SQLModel `Computed()` mirror, so lens predicate tests work in-fixture.
- `adventure_entity` is a VIEW; SQLite `create_all` will not build it. Subgraph/roster tests must query the base tables via the join in `scope_entity_ids` (which they do), not the view, so they pass under SQLite.

## Open item for Joe
Confirm **public tier** is the intended home (design decision 1). If EXPLORE should be private/DM-only instead, Phase D2 collapses to a live `knn_embeddings` call (no precompute table) and the route moves under `src/routes/private/app/grimoire/[campaign]/explore/`, but Phases A-C are unchanged.
