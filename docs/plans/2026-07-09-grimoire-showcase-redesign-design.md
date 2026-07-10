# Grimoire Showcase Redesign (Design)

Date: 2026-07-09
Status: Approved by Joe (brainstorming session)
Scope: Public Grimoire frontend at `/app/grimoire/*` in `projects/monolith/frontend`

## Problem

The 2026-07-09 "seamless UX" round shipped mechanics (pre-settled Explore, in-place
codex, chat constellation) but the inner pages do not deliver the engaging,
delightful experience the scroll-story landing page promises. Specific failures,
from Joe's screenshot review:

- Library: "THE GRIMOIRE" eyebrow repeats the nav wordmark; per-book stats are
  gray-on-gray and hard to read; the copyright lock floats far right, detached
  from meaning.
- Book reader: TOC is a flat list (no hierarchy under e.g. Dragonborn); the
  content column is slim relative to the viewport; the sub-nav breadcrumb strip
  ("A5E SRD ... DRAGONBORN 30/5631") adds nothing.
- Entities: back-arrow to Library duplicates the nav; a wall of identical cards
  ordered by relationship count is weird UX; no reason to browse.
- Explore: graph is a dense hairball cut off at the bottom; the scope control is
  a giant native select; relationship rows read as "Blade of Avernus - owns",
  which is directionally ambiguous; the entity detail card is a huge serif
  title on a gray block with little content.
- Chat: entity mention highlighting shipped as a type-colored underline tint
  (`mention-highlight.js` sets only `text-decoration-color`) and is effectively
  invisible; flat gray message surfaces give poor contrast for the actual text.

Framing decision: the app is an exploratory showcase, not a live-session DM
tool. Delight and atmosphere win over lookup speed. Live session lookup is out
of scope for this UI.

## Decision summary

Option C was chosen: a full narrative re-imagining, with all targeted fixes
folded in as the baseline.

1. Merge Entities + Explore into one entity-centric **World** page.
2. Nav becomes **Library / World / Chat**; all secondary back-links, eyebrows,
   and the reader breadcrumb strip are removed.
3. Use extracted book/entity art throughout (5,751 indexed images).
4. World lands on a curated featured entity, pre-opened.
5. One "you are inside the book" visual and motion system ties the pages to the
   landing page's register.

## Section 1: Concept and journey

The whole app is one book you are inside. The inner pages continue the object
the landing page opened.

- Nav: `GRIMOIRE | Library . World . Chat`. The nav bar picks up the grimoire
  identity (parchment-dark surface, serif wordmark) instead of a plain SaaS
  header. No other wayfinding chrome anywhere.
- Page transitions: a shared ~300ms page-turn transition (slide/fade of the
  content column) between the three sections. Transitions never block input;
  every state stays URL-addressable; `prefers-reduced-motion` gets instant cuts.
- Persistent thread: the session constellation moves from chat-page state to a
  shared store (sessionStorage-persisted). Entities touched anywhere (World
  card opened, reader mention tapped, grounded chat answer) accrue to one
  constellation, shown as a small dock on every page. Clicking a node jumps to
  that entity in World. `constellation-state.js` is already pure functions over
  node_touched events, so this is a lift-and-share, not a rewrite.
- Journey roles: Library = what is in here; World = who/what exists and how it
  connects; Chat = ask the book. Every entity mention everywhere is a live,
  type-colored link into World; every World excerpt links into the reader at
  that chunk.

## Section 2: Library

A shelf, not a table of contents.

- Books as objects: each book renders as a cover card using its best extracted
  image. One-time curation picks a hero image per book (cover_image column or
  config map); books without good art get a generated typographic cover.
  Grouped by kind as today, laid out as a shelf grid.
- Stats: per-book chips (two or three, readable weight) replace the slash-run;
  library totals become one quiet summary line under the title.
- Copyright lock becomes a labeled badge on the card: a bordered chip with the
  lock glyph reading "Reference only". Readable books get a "Read" affordance
  with a slight page-lift hover.
- Readable books are the heroes: full-color covers and priority; reference-only
  books render slightly muted. The All/Readable toggle stays.
- "THE GRIMOIRE" eyebrow removed.

## Section 3: World (Entities + Explore merged)

Pick an entity, see its world.

- Header controls: prominent entity typeahead (37k entities, results grouped by
  type in type colors) + a styled searchable book/adventure combobox replacing
  the native select ("Everything" at top, chapters nested under books) + the
  World/Story/Quests/Rules lens tabs restyled.
- Landing state: curated rotation of iconic featured entities (Strahd, Tiamat,
  Waterdeep, ...) with the card open and the neighborhood graph pre-settled.
- Ego graph, not whole-scope: the graph centers the selected entity, 1-2 hops,
  nodes sized by relevance. Canvas is a flex region filling the viewport below
  the header (fixes bottom cutoff); camera auto-fits with the existing easing.
  Selecting a node re-centers with a smooth glide.
- Directed relationship phrases: outgoing renders "owns **Blade of Avernus**";
  incoming renders "**Zariel** owns this"; symmetric types (ally of) render
  with no direction glyphs. rel_type grouping stays as quiet subheads.
- Card: compact header (type badge, entity art where extraction found an image
  near its mentions, else a type-colored monogram), description, stats,
  relationship phrases, "appears in" excerpts linking into the reader. Docked
  right on wide screens, bottom sheet on narrow.
- The Entities list page dies. Type browsing survives as type filter chips in
  the search dropdown and legend. `/app/grimoire/entities` redirects to World.

## Section 4: Book reader

- Nested TOC: collapsible tree from the existing section_hierarchy data.
  Current position expanded, siblings collapsed by default. Chunk counts
  removed from rows.
- Width: content column at a proper reading measure (~70-75ch), layout centered
  as sidebar + content. Book images render inline at column width where chunks
  reference them.
- Sub-nav strip deleted. Book title heads the TOC column; reading position is a
  thin progress line under the main nav plus the highlighted TOC row.
- Entity mentions in text get type-colored live links; clicking opens the World
  card as an overlay without losing reading position (reuse the in-place codex
  pattern).

## Section 5: Chat

- Message bodies get white card surfaces on the parchment background.
- Mentions switch from underline tint to visible type-colored text with a soft
  tinted background, matching reader and World treatments (change is in the
  span styling emitted by `mention-highlight.js` plus CSS).
- Grounding chips become clickable links into World.
- The constellation panel becomes the shared dock from Section 1.

## Section 6: Motion and visual system

- One `grimoire` theme layer: parchment surfaces, serif display faces, the
  existing `--grim-type-*` color tokens in `lib/grimoire/theme.css`.
- Shared page-turn transition component used by all three sections.
- Motion discipline (carried over from the seamless round): position never
  animates during camera refits; opacity/scale only; `prefers-reduced-motion`
  honored everywhere with instant cuts.

## Out of scope

- Live-session/DM lookup workflows.
- Any private-tier Grimoire surface.
- Extraction pipeline changes (art selection uses already-indexed images; a
  curation pass or heuristic picks per-book/per-entity images).

## Key existing code

- Library: `frontend/src/routes/public/app/grimoire/library/+page.svelte`
- Reader: `frontend/src/routes/public/app/grimoire/book/[book]/+page.svelte`
- Entities: `frontend/src/routes/public/app/grimoire/entities/+page.svelte`
- Explore: `frontend/src/routes/public/app/grimoire/explore/+page.svelte`,
  `lib/public/grimoire/explore/ExploreCanvas.svelte`, `ExploreCodex.svelte`
- Chat: `frontend/src/routes/public/app/grimoire/chat/+page.svelte`,
  `lib/public/grimoire/chat/mention-highlight.js`, `constellation-state.js`,
  `lib/public/grimoire/MiniConstellation.svelte`
