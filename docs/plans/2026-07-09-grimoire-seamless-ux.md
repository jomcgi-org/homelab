# Grimoire Seamless UX Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development in-session) to implement this plan task-by-task.

**Goal:** Ship the three approved UX mockups (artifact `4d10901b`): the Explore graph settles before it paints, the Entities page expands an in-place codex instead of navigating, and Chat grows a live grounded constellation panel.

**Architecture:** All changes live in the public Grimoire SvelteKit frontend (`projects/monolith/frontend/src`). One new shared canvas component (`MiniConstellation.svelte`) renders a pre-settled, fade-in mini graph and is used by both the entities codex and the chat panel. Two new pure JS modules (`mention-highlight.js`, `constellation-state.js`) carry the testable logic. No backend, schema, or API changes: every data need is served by existing endpoints (`/entities/{id}`, `/explore/ego?id=`, chat SSE `node_touched` frames).

**Tech Stack:** Svelte 5 (runes: `$state`, `$props`, `$effect`), canvas 2D, vitest for pure modules, BuildBuddy CI (no local test loop), visual regression on public pages.

**Motion rule (from the approved mockups):** position is never animated on screen; only opacity and scale. Graphs settle off-screen (synchronous ticks) and elements appear at their final position. `prefers-reduced-motion` gets instant cuts everywhere.

---

## Context for a fresh engineer

- Frontend root: `projects/monolith/frontend/src`. Public grimoire routes: `routes/public/app/grimoire/`. Shared grimoire lib: `lib/public/grimoire/`. Theme tokens (`--grim-*`, `--grim-type-*`): `lib/grimoire/theme.css`.
- All public grimoire pages are `ssr = false` and fetch via `lib/public/grimoire/api.js` (`apiFetch`, same-origin proxy `/app/grimoire/api`). Never fetch `/api/...` directly (public origin has no `/api` rule).
- `exploreEgo(id)` returns `{nodes: [{id, name, entity_type}], edges: [{from, to, rel_type}]}` (1-hop neighborhood). `apiFetch("/entities/{id}")` returns the full entity (see `lib/public/grimoire/statblock/EntityDetail.svelte` for its shape).
- Chat grounding: `lib/public/grimoire/chat/stream.js` reduces SSE frames; each `touched[]` item is `{id, title, kind: "chunk"|"entity", entity_type?, book_id?, chunk_ref?}`.
- **No local tests.** Implement, commit; CI runs on push at the end. Do NOT run `bazel test`, `pnpm test`, etc. from the workstation.
- Run `bazel/tools/format/fast-format.sh` before each commit (the bare `format` shim is not on PATH in worktree shells).
- Style: no em-dashes in any copy, comments, or commit messages. Conventional Commits enforced by hook.
- Worktree: `/tmp/claude-worktrees/grimoire-seamless-ux`, branch `feat/grimoire-seamless-ux`.

---

### Task 1: ExploreCanvas settles before the first frame

**Files:**
- Modify: `projects/monolith/frontend/src/lib/public/grimoire/explore/ExploreCanvas.svelte`

The bounce: `startSim()` (line ~312) starts the rAF loop at `energy = 1` from ring-placed initial positions, so the user watches the whole simulation converge. The reduced-motion branch already does the right thing (220 synchronous ticks, then one draw). Generalize it to everyone, then fade NEW nodes in at rest.

**Step 1: Deterministic initial placement**

In the `$effect` layout rebuild (line ~100), replace the index-based ring placement with an id-hash placement so the settled layout is stable across visits, and tag new nodes:

```js
function hashOf(id) {
  const s = String(id);
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}
```

Inside the `nextNodes.map`:

```js
const h = hashOf(n.id);
const angle = ((h % 3600) / 3600) * Math.PI * 2;
const radius = 108 + ((h >>> 12) % 4) * 34;
const node = {
  ...n,
  x: prev?.x ?? Math.cos(angle) * radius,
  y: prev?.y ?? Math.sin(angle) * radius,
  vx: prev?.vx ?? 0,
  vy: prev?.vy ?? 0,
  deg: 0,
  isNew: !prev,
};
```

**Step 2: Replace the live-settle loop with settle-then-fade**

Replace `loopFrame`/`startSim` (keep `tick()` and `draw()` as they are, keep `requestDraw`):

```js
let fadeUntil = 0;

function settleNow() {
  energy = 1;
  for (let i = 0; i < 220; i++) tick();
  energy = 0;
}

function startSim() {
  settleNow();
  if (REDUCED_MOTION) {
    sim.forEach((n) => delete n.bornAt);
    draw();
    return;
  }
  const now = performance.now();
  let i = 0;
  sim.forEach((n) => {
    if (n.isNew) {
      n.bornAt = now + Math.min(i * 14, 350);
      i++;
    }
  });
  fadeUntil = now + 320 + Math.min(i * 14, 350);
  if (!rafId) rafId = requestAnimationFrame(fadeFrame);
}

function fadeFrame() {
  draw();
  if (performance.now() < fadeUntil) {
    rafId = requestAnimationFrame(fadeFrame);
  } else {
    rafId = null;
    sim.forEach((n) => delete n.bornAt);
    draw();
  }
}
```

Note this also fixes ego "wander" merges: existing nodes keep their positions (the `prev?.x` continuity), the settle barely moves them, and only the newly pulled guests fade in.

**Step 3: Fade support in draw()**

At the top of `draw()`: `const now = performance.now();` and a helper:

```js
function fadeOf(n, now) {
  if (!n.bornAt) return 1;
  return Math.max(0, Math.min(1, (now - n.bornAt) / 280));
}
```

- Edges: multiply the existing `ctx.globalAlpha` value by `Math.min(fadeOf(a0, now), fadeOf(b0, now))` where `a0`/`b0` are the sim nodes (before `toScreen`). Skip drawing when that factor is 0.
- Nodes: multiply `ctx.globalAlpha` by `fadeOf(n, now)`, and scale the radius: `const f = fadeOf(n, now); const r = radiusOf(n) * (0.6 + 0.4 * (1 - Math.pow(1 - f, 3)));` (ease-out cubic, appears in place, no travel). Skip when f is 0. Apply the same alpha factor to that node's focus/hover/guest rings.
- Labels: multiply label alpha by `fadeOf(n, now)` (set `ctx.globalAlpha` per label; it is currently left at 1 for the whole label pass).

**Step 4: Sanity-check the interaction paths**

`requestDraw()` guards on `!rafId`, which still works (fade loop owns rafId while fading). Pan/zoom/hover during fade also still works: they call `requestDraw()`, and the fade loop is already drawing every frame. `onMount` calls `startSim()` after `refreshColors()`; the `$effect` rebuild also calls it. Both fine. Confirm nothing else references `loopFrame` or `energy` decay.

**Step 5: Self-review, format, commit**

```bash
bazel/tools/format/fast-format.sh
git add -A && git commit -m "fix(grimoire): settle explore graph off-screen before first paint"
```

---

### Task 2: Shared MiniConstellation component + Entities in-place codex

**Files:**
- Create: `projects/monolith/frontend/src/lib/public/grimoire/MiniConstellation.svelte`
- Create: `projects/monolith/frontend/src/routes/public/app/grimoire/entities/EntityCodexRow.svelte`
- Modify: `projects/monolith/frontend/src/routes/public/app/grimoire/entities/+page.svelte`

**Step 1: MiniConstellation.svelte**

A small, non-interactive, pre-settled canvas graph. Props:

```js
let {
  nodes = [],          // [{id, name, entity_type}]
  edges = [],          // [{from, to}]
  focusId = null,
  revealedIds = null,  // Set of ids to show, or null = show all
} = $props();
```

Behavior (borrow the mechanics from ExploreCanvas, simplified):
- Plain (non-reactive) sim array; deterministic hash placement (same `hashOf` pattern as Task 1); a `$effect` on `nodes`/`edges` rebuilds preserving previous positions by id, then runs 220 synchronous ticks with the SAME force constants as ExploreCanvas's `tick()` but a shorter spring rest length (64) and weaker repulsion (2400) to suit a ~300px stage.
- Fit-to-content: after settling, compute the node bounding box and set `view.k`/`view.x`/`view.y` so it fits with 28px padding (clamp k to [0.5, 1.4]).
- Draw: same node/edge/label style as ExploreCanvas (type colors via `getComputedStyle` custom-property reads off the canvas element, paper-stroked labels in the serif stack, accent ring on `focusId`). Labels at 11px, focus label 12px semibold.
- Reveal: a node whose id is not in `revealedIds` (when a Set is passed) draws at alpha 0; when it first enters the set, stamp `bornAt` and fade/scale in exactly as Task 1 (280ms, ease-out cubic, opacity+scale only). Edges draw only when both ends are revealed, at the min of their fades.
- `prefers-reduced-motion`: no fades, everything revealed draws at full alpha immediately.
- ResizeObserver + devicePixelRatio handling as in ExploreCanvas's `resize()`.
- The canvas is decorative alongside a text list in both consumers: `aria-hidden="true"` on the canvas, plus a visually-hidden text alternative is the consumer's job.

**Step 2: EntityCodexRow.svelte**

Props: `{ entityId, onnavigatehint = null }`. Internal `currentId = $state(entityId)` (a `$effect` resets it when the prop changes), so relationship pills re-target the codex in place without moving the row.

On `currentId` change, fetch in parallel (both best-effort, mirror `ExploreCodex.svelte:54-61`):
- `apiFetch(`/entities/${encodeURIComponent(currentId)}`)` for the detail
- `exploreEgo(currentId)` for relationships + the mini graph

Layout (2-column grid, stacks under 720px):
- Left: type row (swatch + uppercase type label, exactly like `ExploreCodex.svelte:127-130`), serif name, a SHORT summary, relationship pills, then two links: `Full entry →` (`entityHref(currentId)`) and `Open in Explore →` (`${exploreHref()}?focus=${encodeURIComponent(currentId)}`).
  - Summary: read `lib/public/grimoire/statblock/EntityDetail.svelte` first to learn the entity payload's summary/description field name; render the first ~2 sentences (CSS `-webkit-line-clamp: 3` on the paragraph). Do NOT embed the full `EntityDetail` statblock; the codex is a preview, the full entry link is the deep path.
  - Relationship pills: build from ego exactly like `buildRelationships` in `ExploreCodex.svelte:67-82` (copy the resolve logic; it is 15 lines, not worth extracting). Pill = type-color dot + peer name + faint mono rel_type. Click sets `currentId = peer.id`.
- Right: `MiniConstellation` with the ego nodes/edges, `focusId={currentId}`, `revealedIds={null}`, min-height 240px, hairline left border. Give the right cell `aria-hidden="true"` and rely on the pill list as the accessible equivalent.
- Skeleton: reuse the shimmer pattern from the page while loading; error renders inline like `ExploreCodex`.
- Expand animation on the WRAPPER (owned by the page, step 3): the row div animates `max-height` 0 -> 480px and opacity, `cubic-bezier(0.22, 1, 0.36, 1)` 340ms, none under reduced motion.

**Step 3: Wire into entities/+page.svelte**

- Add `let openId = $state(null);`.
- Card `<a>` keeps its `href={entityHref(ent.id)}` (new-tab/middle-click still reaches the full page) but plain clicks expand instead:

```js
function onCardClick(e, id) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1) return;
  e.preventDefault();
  openId = openId === id ? null : id;
}
```

Add `aria-expanded={openId === ent.id}` and `class:sel={openId === ent.id}` (selected card: `border-color: var(--grim-accent); background: var(--grim-accent-soft);`).
- Inside the existing `{#each items as ent (ent.id)}`, AFTER the `</a>`, render:

```svelte
{#if openId === ent.id}
  <div class="codex-row">
    <EntityCodexRow entityId={ent.id} />
  </div>
{/if}
```

`.codex-row { grid-column: 1 / -1; }` puts it on its own grid row directly below the card's row. The open/close max-height animation lives on `.codex-row`.
- Close on Escape (window keydown while `openId`), and clear `openId` inside `load()` (a filter/search reload invalidates the selection).
- Type-filter chips and search behavior are untouched. Do NOT auto-open anything: the initial render must be pixel-identical to today so the `grimoire-entities` visual-regression target only changes when a card is clicked (which the screenshot never does).

**Step 4: Self-review against the mockup**

Codex opens under the card with only opacity/max-height motion; pills re-target in place; ego graph never visibly simulates; cmd-click still navigates; Escape closes; reduced-motion is instant.

**Step 5: Format + commit**

```bash
bazel/tools/format/fast-format.sh
git add -A && git commit -m "feat(grimoire): in-place entity codex with pre-settled ego graph"
```

---

### Task 3: mention-highlight pure module + tests

**Files:**
- Create: `projects/monolith/frontend/src/lib/public/grimoire/chat/mention-highlight.js`
- Create: `projects/monolith/frontend/src/lib/public/grimoire/chat/mention-highlight.test.js`

Post-processes renderMarkdown's OUTPUT (already-escaped HTML with no raw user HTML) to underline touched entity names in their type color. String-level, DOM-free, so it is unit-testable like `markdown.test.js`.

**Step 1: Write the failing tests first**

Check how `markdown.test.js` (or the nearest frontend `*.test.js`) is registered for CI (vitest config / BUILD target) and mirror that registration exactly. Cases:

```js
import { highlightMentions } from "./mention-highlight.js";

const strahd = { id: 7, title: "Strahd von Zarovich", kind: "entity", entity_type: "npc" };

// wraps a plain-text mention with the type-colored span
highlightMentions("<p>Strahd von Zarovich rules.</p>", [strahd])
// -> '<p><span class="gmark" style="text-decoration-color: var(--grim-type-npc, currentColor)">Strahd von Zarovich</span> rules.</p>'

// never touches tag internals: an attribute containing the name is left alone
// matches case-insensitively but preserves the original casing in output
// matches the HTML-escaped form (name "A & B" matches text "A &amp; B")
// ignores touched items with kind !== "entity" and items with empty titles
// regex metacharacters in titles do not throw and do not mis-match
// overlapping titles: longer titles win (sort by length desc before matching)
// idempotent enough for streaming: running it twice does not double-wrap
```

**Step 2: Implement**

```js
const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export function highlightMentions(html, touched) {
  const entities = (touched ?? [])
    .filter((t) => t?.kind === "entity" && t.title)
    .sort((a, b) => b.title.length - a.title.length);
  if (!entities.length) return html;
  // Split into tag and text segments; only rewrite text segments, so markup
  // and attributes can never be corrupted.
  return html
    .split(/(<[^>]*>)/)
    .map((seg) => {
      if (seg.startsWith("<")) return seg;
      let out = seg;
      for (const e of entities) {
        const pat = new RegExp(escapeRegex(escapeHtml(e.title)), "gi");
        out = out.replace(pat, (m) =>
          `<span class="gmark" style="text-decoration-color: var(--grim-type-${e.entity_type ?? "class"}, currentColor)">${m}</span>`,
        );
      }
      return out;
    })
    .join("");
}
```

For the no-double-wrap property, note the split runs on the INPUT html each call while the chat page always calls it on freshly rendered markdown, never on its own output; assert that contract in a test comment and cover the "already contains a gmark span" case by skipping segments between our own spans if the simple approach fails the test (the split approach treats our inserted `<span>` as a tag segment on a second pass, but the text between them would re-match: handle it by checking the previous segment is not our opening span before replacing, or simply document and enforce the fresh-input contract in the caller and drop that test). Prefer the simplest implementation that passes; the caller contract is the real guarantee.

Security note: input is renderMarkdown output (all `&<>` escaped, no raw user HTML), titles are matched in escaped form, and the only inserted markup is our own literal span with a `--grim-type-*` var reference; `entity_type` is interpolated into a CSS var name, so allow-list it (`/^[a-z_]+$/`, else fall back to `class`).

**Step 3: Commit**

```bash
bazel/tools/format/fast-format.sh
git add -A && git commit -m "feat(grimoire): type-colored entity mention highlighting for chat replies"
```

---

### Task 4: Chat grounded constellation panel

**Files:**
- Create: `projects/monolith/frontend/src/lib/public/grimoire/chat/constellation-state.js`
- Create: `projects/monolith/frontend/src/lib/public/grimoire/chat/constellation-state.test.js`
- Modify: `projects/monolith/frontend/src/routes/public/app/grimoire/chat/+page.svelte`

**Step 1: Pure session-constellation state + tests (tests first)**

Edges must be REAL relationships, never fabricated: nodes come from `touched[]`, edges come from best-effort ego fetches intersected with the session's node set.

```js
// constellation-state.js
export function emptyConstellation() {
  return { nodes: [], edges: [], ids: new Set(), egoFetched: new Set() };
}

// touched item -> maybe a new node (kind "entity" only, dedupe by id)
export function withTouched(state, item) { ... }

// ego response for `forId` -> add edges where BOTH ends are session nodes,
// dedupe undirected pairs, mark forId ego-fetched
export function withEgo(state, forId, ego) { ... }
```

Test: chunk items ignored; duplicate entity ids ignored; ego edges to non-session entities dropped; undirected dedupe (`a->b` then `b->a` is one edge); a later-arriving node does NOT retroactively get earlier egos' edges (acceptable v1 looseness, but ego responses are kept OUT of state so re-running `withEgo` later is possible: simplest is to also store `egos: Map<id, ego>` and recompute edges inside `withEgo`; pick whichever keeps the module under ~80 lines and test what you build). Register the test the same way as Task 3's.

**Step 2: Wire into the chat page**

In `chat/+page.svelte`:
- `let constellation = $state(emptyConstellation());`
- In `send()`'s `onFrame`, after `turn = applyFrame(turn, frame)`: if `frame.type === "node_touched"`, run it through `withTouched`; for a newly added entity id, fire `exploreEgo(id)` (import from `api.js`) best-effort: `.then((ego) => { constellation = withEgo(constellation, id, ego); }).catch(() => {})`.
- Reset `constellation` in `applyFreshState()` (NEW CHAT).
- Seed from history: on mount / hydration, fold every `messages[].touched` entity through `withTouched` and fire their ego fetches, so a reloaded session shows its constellation (all revealed instantly, no replay theater).

Layout: wrap the transcript in a new flex row INSIDE `.chat-box`, between `.panel-head` and the notice/input:

```svelte
<div class="chat-main">
  <div class="chat-transcript" bind:this={transcriptEl}> ... unchanged ... </div>
  {#if constellation.nodes.length}
    <aside class="constellation" aria-label="Entities this conversation has drawn on">
      <span class="constellation-cap">SESSION CONSTELLATION</span>
      <MiniConstellation
        nodes={constellation.nodes}
        edges={constellation.edges}
        revealedIds={new Set(constellation.ids)}
      />
    </aside>
  {/if}
</div>
```

CSS: `.chat-main { flex: 1; min-height: 0; display: flex; }`, transcript keeps `flex: 1; min-height: 0;`. Panel: `flex: 0 0 280px; border-left: 1px solid var(--grim-line); background: var(--grim-paper); position: relative;` with a one-time slide-in (`animation: constel-in 0.45s cubic-bezier(0.22, 1, 0.36, 1)` from `flex-basis: 0` is unreliable; animate `width`/`opacity` via a keyframe on the aside instead, and set `overflow: hidden`). Reveal of individual nodes is MiniConstellation's job (`revealedIds` grows as touched frames arrive). Below 900px: `display: none` (the GROUNDED IN chips remain the mobile grounding surface; note this in a comment). Reduced motion: no slide-in animation.

The cap label styles like the page's existing eyebrows (mono, 9.5px, letter-spacing 0.13em, `--grim-text-faint`).

**Step 3: Mention underlines**

In `renderReply` (chat/+page.svelte:92):

```js
function renderReply(text, touched) {
  return highlightMentions(renderMarkdown(text ?? "", new Map()), touched ?? []);
}
```

Call sites: committed messages pass `m.touched`, the streaming turn passes `turn.touched`. Add `.turn-md :global(.gmark) { text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 2px; }` (color comes from the inline `text-decoration-color` var).

**Step 4: Self-review against the mockup**

Panel absent until first grounded entity; appears once with a single slide; nodes pop in at settled positions as the stream touches them; edges only between real relationships; NEW CHAT resets it; reloaded sessions show it fully revealed; mobile hides it; reduced motion is instant. The `/chat` visual target is the notes surface and `grimoire-home` does not render this page, so no visual fixtures change.

**Step 5: Format + commit**

```bash
bazel/tools/format/fast-format.sh
git add -A && git commit -m "feat(grimoire): live grounded constellation panel in chat"
```

---

### Task 5: Chart bumps

Frontend changes deploy on the public tier: `jomcgi.dev` is served by `monolith-public`, and per the public-tier checklist BOTH charts bump in the same PR.

```bash
cd /tmp/claude-worktrees/grimoire-seamless-ux
bazel/tools/git/bump-chart.sh projects/monolith
bazel/tools/git/bump-chart.sh projects/monolith-public
bazel/tools/format/fast-format.sh
git add -A && git commit -m "chore(grimoire): bump monolith + monolith-public charts"
```

(If `monolith-public`'s chart lives elsewhere, find it with `ls projects/ | grep public`; memory says both bumps are required for any public-page rollout.)

---

### Task 6: PR, review, CI, merge, live verification

1. Push: `git push -u origin feat/grimoire-seamless-ux`.
2. Open the PR (`gh pr create`) describing the three UX changes, linking the mockup artifact rationale in prose.
3. **One comprehensive code review of the full diff** (Opus reviewer, code-reading only) before relying on CI: focus on the ExploreCanvas rewrite regressions (pan/zoom/hover during fade, ego merge path), XSS surface of `highlightMentions` (must only ever wrap already-escaped text segments), Svelte 5 rune correctness, and reduced-motion coverage.
4. Watch CI: `gh pr checks <n> --watch`; on failure, `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`, quote the real error before hypothesizing. Expect the visual-regression action to post before/after images for `grimoire-entities` (should be no-diff) and `grimoire-home`.
5. Merge: `gh pr merge --auto --rebase` (rebase only; if "clean status", merge directly).
6. Verify live after ArgoCD syncs (public tier: render/content signal, not just 200s):
   - `curl -sS -o /dev/null -w '%{http_code}\n' https://jomcgi.dev/app/grimoire/entities` (200)
   - Eyeball `https://jomcgi.dev/app/grimoire/explore` (graph fades in settled, no bounce), the entities codex, and a chat turn's constellation.
