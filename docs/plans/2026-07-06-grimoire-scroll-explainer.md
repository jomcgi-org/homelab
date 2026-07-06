# Grimoire Scroll Explainer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (repo default; do not offer the two-option handoff). Tasks 0 and 1 are interactive and run in the MAIN session with Joe, not in subagents. Per repo CLAUDE.md: defer all test execution to end-of-PR CI; one comprehensive code review per PR at the end, not per task.

**Goal:** Replace the static hero + pipeline section of the public `/app/grimoire` landing with a scroll-scrubbed "From Scan to Sage" explainer: a real scanned page visibly becomes layout boxes, chunks, entities, a mini graph, and finally a grounded chat replay.

**Architecture:** A tall scroll region wraps a `position: sticky` full-viewport stage. One rAF loop maps scroll progress to a master timeline `t in [0,1]`; a pure `timeline.js` module (unit-tested) converts `t` into per-element keyframed layouts; `ScrollStory.svelte` applies them imperatively to plain (non-reactive) DOM refs, mirroring the `ExploreCanvas.svelte` discipline. All data is baked into the bundle (zero runtime fetches). Reduced-motion/no-JS renders six static scenes.

**Tech Stack:** SvelteKit 2 / Svelte 5 runes, vanilla rAF + CSS transforms (no GSAP, no new deps), vitest (auto-globbed `src/**/*.test.js`), existing `.grimoire` design tokens.

**Design doc:** `docs/plans/2026-07-06-grimoire-scroll-explainer-design.md`

---

## Ground rules for every task

- Work in this worktree (`/tmp/claude-worktrees/grimoire-scroll-explainer`), branch `feat/grimoire-scroll-explainer`.
- **No em-dashes** in any copy, comments, or commit messages.
- **Do not run tests locally** (no `pnpm test`, no `bazel test`). Write tests, commit, push; CI runs them.
- Run `bazel/tools/format/fast-format.sh` before each commit (the pre-commit hook also enforces it; if it modifies files, `git add -A` and commit again).
- Conventional Commits, e.g. `feat(grimoire): ...`.
- All new frontend code lives under `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/` plus edits to `routes/public/app/grimoire/+page.svelte`.
- Match the existing landing's idiom: `.grimoire` CSS custom properties only (`--grim-*`), serif titles via `grim-title`, `prefers-reduced-motion` guards, no hard-coded colors.
- The landing must keep making **zero `/api/grimoire` fetches**. The baked showcase excerpt is a deliberate, Joe-approved exception to "no corpus content outside the gate"; Task 8 updates the page comment to say so.

---

## Task 0: Curate the showcase page and bake the asset bundle (MAIN SESSION, interactive)

> **STATUS: DONE (2026-07-06), kept for the record with corrections.** The chosen page is
> Lost Mine of Phandelver marker page 49 ("Nezznar the Black Spider", the villain reveal):
> 7 chunks, 21 entities, 42 edges, dramatic full-page art. Joe wants the page choice to stay
> swappable, so the bake is a committed, parameterized script:
> `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/bake-scrollstory.sh <book_id> <page> <pdf_path>`.
> Corrections to the original steps discovered while executing:
> - `monolith-run-python` is a zero-egress sandbox with NO database or network access.
>   All corpus reads go through `kubectl exec -i monolith-pg-1 -c postgres -- psql`.
> - The pipeline stores no page scans: `image_ref` is per-chunk art crops. The page scan
>   is rendered from a LOCAL PDF (`pdftoppm`), found on Joe's Mac via `mdfind`.
> - No hand-traced bboxes needed: marker's full layout output (real per-block bboxes in
>   page pixel space, block ids identical to `chunk_ref`) lives in SeaweedFS at
>   `/buckets/grimoire/books/<book>/raw/output.json.gz`; the script fetches it via
>   `kubectl exec` on `seaweedfs-filer-0` and normalizes to fractional coords.
> - Chunks whose section starts on an earlier page yield bboxes with `chunkId: null`;
>   the story fades those out instead of flying them.
> - Corpus totals baked for the scale phase: 33 books, 40,514 chunks, 33,792 entities,
>   45,401 relationships, plus per-type counts.
> - STILL PENDING from this task: the scripted chat transcript (`data/transcript.js`,
>   hand-curated; demo question: "Who is the Black Spider and what does he actually want?").
>   Capture one real sage answer, e.g. `kubectl exec` curl against the grimoire chat
>   backend, and record the GROUNDED IN entity ids.

This task is judgment + live-cluster work; do it in the main session with Joe in the loop. Everything downstream consumes its output, so it goes first.

**Files (output):**
- Create: `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/data/page.webp`
- Create: `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/data/story.js` (bboxes, chunks, entities, edges, transcript as plain exported objects)

**Step 1: Mine the corpus for candidate pages**

Use `mcp__claude_ai_homelab__monolith-run-python` (it has DB access) to run a query over the `grimoire` schema. Candidate = a page image whose chunks are entity-dense and diverse. Shape (adjust to the real mention table names found in `projects/monolith/grimoire/`):

```sql
-- per image_ref: chunk count, distinct entities mentioned, distinct entity
-- types, and edges among co-mentioned entities
SELECT c.book_id, c.image_ref,
       count(DISTINCT c.id) AS chunks,
       count(DISTINCT m.entity_id) AS entities,
       count(DISTINCT e.entity_type) AS types,
       min(c.section_path) AS section
FROM grimoire.knowledge_chunk c
JOIN grimoire.entity_mention m ON m.chunk_id = c.id
JOIN grimoire.entity e ON e.id = m.entity_id
WHERE c.image_ref IS NOT NULL
GROUP BY c.book_id, c.image_ref
HAVING count(DISTINCT e.entity_type) >= 4
ORDER BY count(DISTINCT m.entity_id) DESC
LIMIT 25;
```

Then for the top ~10, pull relationship counts among their co-mentioned entities (join the relationships table both ends against the page's entity set). Score: 4+ types, 8-15 entities, several real edges, and a section that suggests a fun demo question.

**Step 2: Shortlist 3-5 pages and let Joe pick**

For each finalist fetch the page image (`GET /api/grimoire/chunks/{id}/image` on the public tier, or presign via run-python) into the scratchpad, view them, and present Joe a comparison (page thumbnail description, entity roster, proposed demo question each). Use AskUserQuestion. LMoP is the default pool; all books compete.

**Step 3: Export the data**

Via run-python, export for the chosen page: chunk ids, `seq`, `section_path`, `content`; entities (id, name, entity_type, one-line detail); relationships among them (src, dst, rel label). Save raw JSON to scratchpad.

**Step 4: Produce bounding boxes**

No bboxes exist in the schema. Produce them for this one page:
- Read the page image directly (Claude can view it) and write a first-pass `bboxes` array: `{ id, kind: "header" | "text" | "aside" | "art", x, y, w, h, chunkId }` in fractional page coordinates (0..1, so they scale with any render size).
- Refine visually: build a throwaway HTML page in the scratchpad that renders the image with the boxes overlaid, publish via Artifact, and iterate with Joe until boxes look like the datalab screenshot (red headers, blue text, orange asides).
- Each box maps to the chunk whose content it belongs to (several boxes can share a chunkId).

**Step 4b: Export corpus totals for the scale phase**

Via run-python: total books, chunks, entities, and the per-type entity counts
(`SELECT entity_type, count(*) FROM grimoire.entity GROUP BY 1`). Round the
headline numbers for copy; keep exact per-type proportions for sampling the
constellation types.

**Step 5: Capture the scripted chat transcript**

Agree the demo question with Joe (it should be answerable from this page's entities). Ask the live public sage once (curl the public chat endpoint or via run-python calling the chat backend), and record: user question, assistant answer text, and the GROUNDED IN items with their entity ids/types. Trim the answer if long; it will be typed out character by character.

**Step 6: Bake the bundle**

- Convert the page image to webp at ~1200px wide (`cwebp` or Python PIL via run-python; keep it under ~250 KB).
- Write `data/story.js` exporting plain objects: `pageImage` (imported asset), `bboxes`, `chunks`, `entities`, `edges`, `transcript`, `demoQuestion`, plus `attribution` (book title, page, publisher) rendered near the scan.

```js
// data/story.js shape (values come from the curation pass)
import pageImage from "./page.webp";

export const page = { image: pageImage, aspect: 0.773 /* w/h */ };
export const attribution = { book: "...", page: 0, note: "excerpt shown for demonstration" };
export const bboxes = [ { id: "b1", kind: "header", x: 0.09, y: 0.14, w: 0.2, h: 0.03, chunkId: "c1" } /* ... */ ];
export const chunks = [ { id: "c1", sectionPath: "Introduction / Running the Adventure", text: "..." } /* ... */ ];
export const entities = [ { id: "e1", name: "...", type: "location", detail: "..." } /* ... */ ];
export const edges = [ { from: "e1", to: "e2", label: "located in" } /* ... */ ];
export const mentions = [ { chunkId: "c1", entityId: "e1", phrase: "Neverwinter" } /* ... */ ];
export const transcript = { question: "...", answer: "...", groundedIn: ["e1", "e3"] };
// Corpus totals for the scale phase: snapshot at bake time, rounded for copy.
// Also export a sampled constellation: up to ~1500 {type} entries drawn from
// the real per-type entity distribution (positions are generated at mount
// from a seeded PRNG, so only types need baking).
export const corpus = { books: 33, chunks: 40000, entities: 9000, asOf: "2026-07" };
export const constellation = [ { type: "creature" } /* ...sampled... */ ];
```

**Step 7: Commit**

```bash
git add projects/monolith/frontend/src/lib/public/grimoire/scrollstory/data/
git commit -m "feat(grimoire): bake scroll-story showcase page assets"
```

---

## Task 1: Choreography mockup as an Artifact (MAIN SESSION, interactive)

De-risk the pinned-stage choreography before any Svelte work. Per Joe's standing preference, design iteration happens via artifact mockups.

**Step 1:** Load the `artifact-design` skill, then build a single self-contained HTML file in the scratchpad implementing the full six-phase scrub with the real Task 0 data inlined (image as data URI). Hand-rolled rAF + sticky stage, exactly the mechanics the Svelte port will use.

**Step 2:** Publish with the Artifact tool; iterate with Joe on pacing, easing, and per-phase visuals until approved. Capture the agreed phase boundaries and easings as constants; they feed Task 2 verbatim.

**Step 3:** Nothing committed to the repo from this task except updating the phase constants in the plan notes if they changed.

---

## Task 2: Pure timeline module with tests (TDD)

The scrub math is the only genuinely unit-testable part; isolate it from the DOM.

**Files:**
- Create: `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/timeline.js`
- Create: `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/timeline.test.js`

Tests are auto-picked-up: the frontend `BUILD` globs `src/**/*.test.js` into the vitest target. Follow the import style of `src/lib/public/grimoire/renderChunk.test.js`.

**Step 1: Write the failing tests**

```js
// timeline.test.js
import { describe, expect, it } from "vitest";
import { PHASES, phaseAt, progressIn, lerp, easeInOut } from "./timeline.js";

describe("timeline", () => {
  it("covers [0,1] with contiguous non-overlapping phases", () => {
    let end = 0;
    for (const p of PHASES) {
      expect(p.start).toBeCloseTo(end, 5);
      expect(p.end).toBeGreaterThan(p.start);
      end = p.end;
    }
    expect(end).toBeCloseTo(1, 5);
  });

  it("phaseAt returns the phase containing t, clamped at the ends", () => {
    expect(phaseAt(-0.5).id).toBe(PHASES[0].id);
    expect(phaseAt(0).id).toBe("hero");
    expect(phaseAt(0.999).id).toBe("chat");
    expect(phaseAt(2).id).toBe("chat");
  });

  it("progressIn maps a phase sub-range to 0..1, clamped", () => {
    const p = { start: 0.2, end: 0.4 };
    expect(progressIn(p, 0.1)).toBe(0);
    expect(progressIn(p, 0.2)).toBe(0);
    expect(progressIn(p, 0.3)).toBeCloseTo(0.5);
    expect(progressIn(p, 0.5)).toBe(1);
  });

  it("lerp and easeInOut behave at the boundaries", () => {
    expect(lerp(10, 20, 0)).toBe(10);
    expect(lerp(10, 20, 1)).toBe(20);
    expect(easeInOut(0)).toBe(0);
    expect(easeInOut(1)).toBe(1);
    expect(easeInOut(0.5)).toBeCloseTo(0.5);
  });
});
```

**Step 2: Implement `timeline.js`**

Phase boundaries come from the Task 1 mockup; the defaults below are the starting point.

```js
// timeline.js: pure scrub math for the landing scroll story. No DOM, no
// Svelte; ScrollStory.svelte feeds it a scroll fraction and applies the
// results imperatively.

export const PHASES = [
  { id: "hero", start: 0.0, end: 0.1 },
  { id: "layout", start: 0.1, end: 0.28 },
  { id: "chunks", start: 0.28, end: 0.46 },
  { id: "entities", start: 0.46, end: 0.64 },
  { id: "scale", start: 0.64, end: 0.8 },
  { id: "chat", start: 0.8, end: 1.0 },
];

export function phaseAt(t) {
  const c = Math.min(Math.max(t, 0), 1);
  return PHASES.find((p) => c >= p.start && c < p.end) ?? PHASES[PHASES.length - 1];
}

export function progressIn(phase, t) {
  return Math.min(Math.max((t - phase.start) / (phase.end - phase.start), 0), 1);
}

export const lerp = (a, b, t) => a + (b - a) * t;

export const easeInOut = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);
```

Add the per-phase layout functions as they firm up in Tasks 4-7 (e.g. `boxDrawProgress(i, p)` for staggered bbox reveal, `chunkFlight(i, p)` returning source/target rects). Keep every one pure and add a small test alongside.

**Step 3: Commit**

```bash
git add projects/monolith/frontend/src/lib/public/grimoire/scrollstory/timeline*
git commit -m "feat(grimoire): scroll-story timeline math with tests"
```

---

## Task 3: ScrollStory stage skeleton

**Files:**
- Create: `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/ScrollStory.svelte`

**Step 1: Build the scroller + sticky stage + scrub loop**

Core mechanics (port from the approved mockup):

```svelte
<script>
  import { onMount } from "svelte";
  import { PHASES, phaseAt, progressIn } from "./timeline.js";
  import * as story from "./data/story.js";

  // Reactive: coarse phase only (drives captions and aria state).
  let phaseId = $state("hero");
  let reduced = $state(false);

  // Plain refs mutated per-frame; deliberately NOT runes (same discipline
  // as ExploreCanvas: 60fps writes must not go through the reactivity proxy).
  let scrollerEl, stageEl;
  let raf = 0;

  function frame() {
    raf = 0;
    const r = scrollerEl.getBoundingClientRect();
    const span = r.height - window.innerHeight;
    const t = span > 0 ? Math.min(Math.max(-r.top / span, 0), 1) : 0;
    const phase = phaseAt(t);
    if (phase.id !== phaseId) phaseId = phase.id;
    applyFrame(t, phase, progressIn(phase, t));
  }

  function applyFrame(t, phase, p) {
    // Per-phase imperative transforms on element refs; filled in Tasks 4-7.
  }

  onMount(() => {
    reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return;
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(frame); };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    frame();
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  });
</script>

{#if reduced}
  <!-- Task 8: static stacked scenes -->
{:else}
  <div class="scroller" bind:this={scrollerEl}>
    <div class="stage" bind:this={stageEl}>
      <!-- layered stage children, Tasks 4-7 -->
    </div>
  </div>
{/if}

<style>
  .scroller { height: 600vh; position: relative; }
  .stage {
    position: sticky; top: 0; height: 100vh; overflow: hidden;
    display: grid; place-items: center;
  }
</style>
```

Notes for the implementer:
- SSR renders the initial markup; all measurement is inside `onMount`. Never touch `window` at module scope (prod is SSR; see the ssr.noExternal history for why the public tier is sensitive to this).
- No new npm deps.
- Add a thin progress rail (fixed, right edge) showing the six phase names; clicking one scrolls to that phase's start (`scrollerEl.offsetTop + start * span`).

**Step 2: Mount it temporarily** at the top of `routes/public/app/grimoire/+page.svelte` behind nothing (it will be integrated properly in Task 8), verify it renders in local dev (`pnpm dev` is fine for eyeballing; it is not a test run).

**Step 3: Commit** `feat(grimoire): scroll-story sticky stage skeleton`.

---

## Task 4: Phases 1-2, hero + layout detection

**Files:** Modify `ScrollStory.svelte` (stage children + `applyFrame`), extend `timeline.js` (+ tests) with `boxDrawProgress`.

**Step 1:** Stage children: the page scan as an `<img>` (from `story.page.image`) centered, slight initial tilt (`rotate(-2deg)`) and desk shadow; an absolutely-positioned overlay `<div>` matching the image box, containing one `<div class="bbox bbox-{kind}">` per `story.bboxes` entry, positioned with fractional coords (`left: x*100%` etc.).

**Step 2:** Hero phase (`t` 0-0.12): headline "Your sourcebooks, read and understood." + one-line sub + scroll cue over the dimmed scan; as `p` rises the copy fades up and out, the scan straightens (`rotate` to 0) and scales from 0.92 to 1.

**Step 3:** Layout phase: boxes draw in staggered order (stagger by index via `boxDrawProgress(i, p)`: each box scales from `scaleY(0)` at its own sub-window, border first then fill tint). Colors: header = `--grim-type-creature` red tint, text = `--grim-type-spell` blue tint, aside = `--grim-type-npc` orange tint (matches the datalab screenshot palette using existing tokens). Caption strip at the bottom: "1 / Layout detection: marker reads the page structure."

**Step 4:** Keep attribution (`story.attribution`) as a small faint line under the scan at all times.

**Step 5:** Commit `feat(grimoire): scroll-story hero and layout phases`.

---

## Task 5: Phase 3, chunking flight

**Files:** Modify `ScrollStory.svelte`; extend `timeline.js` (+ tests) with `chunkFlight(i, p)` returning interpolated rects.

**Step 1:** Add a chunk-card column (right half of the stage, hidden until this phase): one card per `story.chunks` entry with `sectionPath` breadcrumb (mono, faint) and the chunk text (clamped to ~4 lines).

**Step 2:** Flight: for each chunk, its bboxes converge and morph into the card. Implementation: FLIP-style; measure each bbox's on-screen rect and its target card rect once per resize, then per frame set `transform: translate(...) scale(...)` interpolating source to target with `easeInOut`, staggered by index. The bbox border radius eases to the card radius, background eases to `--grim-surface`. Meanwhile the scan scales down toward the left and dims to 35% opacity.

**Step 3:** Caption: "2 / Structural chunking: blocks become chunks in reading order, keyed by section." Commit `feat(grimoire): scroll-story chunking phase`.

---

## Task 6: Phase 4, entity extraction + mini graph

**Files:** Modify `ScrollStory.svelte`; extend `timeline.js` (+ tests) with deterministic node layout `graphLayout(entities, edges)` (precomputed positions, no live physics needed; compute once with a few relaxation iterations at mount, or bake positions into `story.js` from the mockup).

**Step 1:** Within each chunk card, wrap each `story.mentions` phrase in a `<mark class="mention mention-{type}">`; as `p` rises the marks tint in their `--grim-type-*` color.

**Step 2:** Chips pop: a chip per entity (`name` + type dot) animates from its mention's position to its graph node position (FLIP again). Chunk cards compress and fade to 25%.

**Step 3:** Edges: an SVG layer under the chips; each edge is a line whose `stroke-dashoffset` animates in as its endpoints arrive, with the `label` on hover only. Node visual language mirrors ExploreCanvas: type-colored filled circles behind the chip labels.

**Step 4:** Caption: "3 / Entity extraction: an LLM reads each chunk and emits typed entities and relationships." Commit `feat(grimoire): scroll-story entity extraction phase`.

---

## Task 6b: Phase 5, the scale pull-back

**Files:** Modify `ScrollStory.svelte`; extend `timeline.js` (+ tests) with `countUp(target, p)` (monotonic, 0 at p=0, exact target at p=1, tabular-friendly integer output).

**Step 1: Constellation canvas.** Add a full-stage `<canvas>` layer behind the graph. At mount, generate positions for `story.constellation` entries with a seeded PRNG (no `Math.random` naked; seed a small mulberry32 so SSR/CSR and repeat visits render identically) in a loose galaxy-disc distribution around stage center. Each dot: 1.5-2.5px, its `--grim-type-*` color at low alpha.

**Step 2: Pull-back choreography.** As `p` rises: the page's mini graph scales down toward stage center (becoming just another bright cluster), chunk-card remnants fade fully, and constellation dots fade in radially outward (near dots first) with a slight parallax drift. Draw imperatively in the existing rAF frame; cap constellation at ~1500 dots.

**Step 3: Counters.** Three large tabular-numeral counters fade in staggered, driven by `countUp`: "N books", "X chunks", "Y entities" (values and rounding from `story.corpus`), with a faint "as of {asOf}" note. Copy line: "You just watched one page. Every book on the shelf gets the same treatment."

**Step 4:** Caption: "4 / The whole shelf: every page, chunked, extracted, connected." (Renumber the chat caption to 5.) Commit `feat(grimoire): scroll-story scale pull-back phase`.

---

## Task 7: Phase 6, chat finale

**Files:** Modify `ScrollStory.svelte`; extend `timeline.js` with `typedChars(text, p)` (+ test: returns 0 chars at 0, full length at 1, monotonic).

**Step 1:** The constellation dims to 30% and keeps a slow drift; the showcase graph cluster stays faintly visible. A chat panel rises from the bottom: the user question appears first (as a sent bubble), then the answer types out driven by `p` (scrub-scrubbed, so scrolling backwards untypes; that is the delight moment, keep it).

**Step 2:** GROUNDED IN chips render under the answer once typing passes 90%; each chip, as it appears, pulses the matching graph node (same id) with a ring in its type color. Reuse the `.touched-chip` visual style from `routes/public/app/grimoire/chat/+page.svelte` (copy the few CSS rules; do not import the page).

**Step 3:** Final beat (`p` > 0.95): CTA row fades in: "Ask the sage" -> `/app/grimoire/chat`, "Wander the graph" -> `/app/grimoire/explore`, "Browse the library" -> existing `libraryHref()`. Caption: "4 / Grounded answers: every claim cites the chunks and entities it came from."

**Step 4:** Commit `feat(grimoire): scroll-story chat finale`.

---

## Task 8: Static fallback, mobile, and page integration

**Files:**
- Modify: `projects/monolith/frontend/src/routes/public/app/grimoire/+page.svelte`
- Modify: `ScrollStory.svelte`

**Step 1: Reduced-motion / no-JS fallback.** In the `{#if reduced}` branch (and as the SSR-first render before hydration), render the six phases as stacked static scenes: scan with boxes already drawn, chunk cards, entity graph (static SVG), corpus counters with static constellation, chat transcript fully typed. Same content, no scrub. Verify by toggling reduced motion in devtools.

**Step 2: Mobile (<= 640px).** Keep the scrub but simplify: scan full-width, chunk cards stack over it instead of beside it, graph smaller (cap visible entities at ~8 by degree), captions larger. Test in devtools responsive mode.

**Step 3: Integrate into the landing.** In `+page.svelte`:
- Replace the hero section and the "How it works" pipeline block with `<ScrollStory />` (the captions carry the pipeline explanation now).
- Keep, condensed: feature grid, grant demo, roadmap, foot-note, below the story.
- Update the top-of-file comment: the page remains fetch-free, but now deliberately embeds ONE curated corpus page excerpt (Joe-approved) as baked assets; any further corpus content still belongs behind the gate.

**Step 4:** Local dev eyeball pass (`pnpm dev`), both themes (the story must read correctly under `.grimoire.dark`; use tokens everywhere and check the scan against the dark paper background, e.g. a subtle light frame behind it).

**Step 5:** Commit `feat(grimoire): land scroll story on public landing`.

---

## Task 9: Chart bumps, PR, review, CI, merge

**Step 1: Chart bumps.** The apex is served by the public tier and frontend changes ship in the monolith image; per the standing gotcha both bumps go in this PR:

```bash
bazel/tools/git/bump-chart.sh projects/monolith
bazel/tools/git/bump-chart.sh projects/monolith-public
git add -A && git commit -m "chore(monolith,monolith-public): bump charts for grimoire scroll story"
```

**Step 2: Push and open the PR.**

```bash
git push -u origin feat/grimoire-scroll-explainer
gh pr create --title "feat(grimoire): scroll-scrubbed 'From Scan to Sage' public landing" --body "..."
```

PR body: link the design doc, one paragraph on the arc, note the deliberate single-page corpus excerpt decision, and the reduced-motion fallback. End with the standard generated-with footer.

**Step 3: One comprehensive code review** of the full PR diff (Opus reviewer, code-reading only), fix findings, push.

**Step 4: Watch CI**: `gh pr checks <number> --watch`; on failure read logs via `mcp__buildbuddy__get_invocation` (commitSha selector) and quote the real error before hypothesizing. Iterate.

**Step 5: Merge** with `gh pr merge --rebase` once green, then poll the ArgoCD rollout (`kubectl get applications -n argocd`) and load `https://jomcgi.dev/app/grimoire` to confirm the story is live in both themes.

**Step 6:** Post-merge, message Joe via `monolith-monolith-agent-notify` only if he is not in the session: one line, the landing is live.

---

## Verification checklist (end of plan)

- [ ] Scrolling the landing scrubs smoothly through all six phases, forwards and backwards, at 60fps on a mid laptop.
- [ ] Scale phase counters match the baked corpus snapshot and the constellation stays smooth (~1500 dots max).
- [ ] `prefers-reduced-motion` shows the static stacked version; content parity with the animated one.
- [ ] Zero `/api/grimoire` requests from the landing (network tab).
- [ ] Dark and light themes both read well.
- [ ] Mobile (390px) is usable and the scrub still works.
- [ ] CI green; vitest timeline tests pass in CI.
- [ ] Both charts bumped in the same PR; ArgoCD synced; live on jomcgi.dev.
