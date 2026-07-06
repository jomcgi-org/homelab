# Grimoire public landing: "From Scan to Sage" scroll explainer

**Date:** 2026-07-06
**Status:** Approved design
**Surface:** Public `/app/grimoire` landing page (monolith-public tier)

## Goal

Replace the static hero and 4-step pipeline section of the public Grimoire landing
with a scroll-driven interactive explainer that shows, not tells, what the pipeline
does: a real scanned book page visibly becomes structured, useful data as the user
scrolls. The north star is delight: "scanned image to useful data?!"

## Decisions made

| Question | Decision |
| --- | --- |
| Surface | Public landing only; the authed home stays a working dashboard |
| Data source | Real data from one curated showcase page, baked into the bundle at build time (zero runtime fetches, preserving the Turnstile-free constraint) |
| Finale | Scripted chat replay (canned but real sage exchange), not a live chat box |
| Copyright | A real book page excerpt is acceptable (LMoP or better candidate from curation) |
| Scroll mechanics | Sticky pinned stage with scroll-scrubbed timeline, hand-rolled (no GSAP, no new deps) |

## Story arc (five phases on one pinned stage)

1. **Hero.** The scanned page fills the stage, slightly tilted like it's on a desk.
   One-line value prop. Scroll cue.
2. **Layout detection.** Datalab-style bounding boxes draw themselves over the scan:
   red section headers, blue text blocks, orange asides. Caption: "reading the page."
3. **Chunking.** Boxes lift off the page and fly right, stacking into clean chunk
   cards with `section_path` breadcrumbs; the scan shrinks and dims behind them.
   This is the "image becomes data" moment.
4. **Entity extraction.** Words inside the chunk cards highlight in the existing
   `--grim-type-*` colors, pop out as entity chips, and edges draw between them
   until they settle into a mini knowledge graph (same visual language as the
   public Explore canvas).
5. **Chat finale.** The graph drifts to the background; a scripted chat replay types
   a question about this exact page; the answer streams in and its GROUNDED IN chips
   light up the matching graph nodes. CTAs into the real `/explore` and `/chat`.

The existing landing's feature grid and roadmap remain below the finale in condensed
form. The explainer replaces the current hero and static pipeline section.

## Scroll mechanics

- A tall scroll region (~600vh) wraps a `position: sticky` full-viewport stage.
- A single `requestAnimationFrame` loop reads scroll progress into a master timeline
  `t in [0,1]`; each phase owns a sub-range with eased interpolation.
- Shared elements tween between keyframed layouts: a bbox becomes a chunk card,
  a highlighted mention becomes an entity chip, chips become graph nodes.
- Per-frame values live in plain non-reactive state; Svelte 5 runes are used only
  for phase-level UI. This mirrors the discipline in
  `src/lib/public/grimoire/explore/ExploreCanvas.svelte`, which runs a 60fps physics
  loop on plain arrays for the same reason.
- Fallbacks: `prefers-reduced-motion` (and no-JS) renders the five phases as static
  stacked scenes. Mobile keeps the scrub with simplified transforms and larger type.

## Data: baked assets, with a curation pass first

### Phase 0: find the hero page

No bounding boxes exist in the schema (`grimoire.knowledge_chunk` stores chunk text,
`seq`, `section_path`, and a whole-page `image_ref`; layout boxes were never
persisted). The showcase page is a curation decision:

- Query the corpus for candidate pages: chunks with `image_ref`, entity mention
  density and type diversity per page, and relationships among co-mentioned entities.
- Shortlist 3 to 5 pages scoring well on: visually interesting scan, 4+ entity types,
  genuinely interconnected entities, and a natural "wow" demo question.
- Joe picks the winner. LMoP is the default pool but all loaded books are scanned.

### Baked asset bundle

One static bundle imported by the landing route (no runtime API calls):

- Page image, optimized (webp).
- Bbox JSON from a one-off marker/datalab layout run on the chosen page,
  hand-tweaked if needed. Fallback: hand-trace ~15 boxes on one page.
- Chunk texts + section paths for that page.
- Entities + relationships JSON (ids, types, names, edges) for the mini graph.
- Scripted chat transcript: a real sage answer captured once, with its GROUNDED IN
  entity ids for the chip-to-node linking.

## Risks and notes

- The bbox layout run is the only genuinely new data work; everything else exists.
- The pinned stage choreography (easing between phases) is the hardest part to get
  right. Iterate on it with artifact mockups before committing the Svelte
  implementation.
- Rollout needs the monolith-public chart bump (apex is served by the public tier;
  both bumps if shared chart changes).
