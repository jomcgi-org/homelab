# Design: Public /app/firecracker scroll explainer

Date: 2026-07-08
Status: Approved (brainstorming session with Joe)

## Problem

The private Firecracker demos page (`/demos/firecracker`) is the best explanation
we have of the fc-invoke daemon, but it runs live sandboxes on node-4 and cannot
be made public due to node headroom. We want a public `jomcgi.dev/app/firecracker`
explainer with the clarity of the Grimoire scroll landing that makes the daemon
magic (snapshot restore from disk to memory, repeatedly) incredibly clear, with
zero cluster load at request time.

## Decision summary

- **Approach A**: standalone sibling page, pattern-copy from the Grimoire
  ScrollStory, no shared-framework extraction. The reusable scaffold (sticky
  stage + rAF scrub) is tiny; the bulk of any scroll story is scene-specific
  choreography, so a framework refactor buys nothing for v1 and risks a live,
  carefully tuned page.
- **Data source**: baked real traces plus a refresh script. Timings shown are
  real recorded fc-invoke span durations, not representative numbers.
- **Format**: scroll-scrubbed story with an interactive replay finale.
- **v1 story scope**: cold boot vs restore, snapshot anatomy, replay finale.
  (Fleet economics beat and per-workload tour deferred; candidates for v2.)
- **Hard requirement**: the scrub must roll fluidly (60fps discipline, see
  Performance below).

## 1. Route and tier wiring

- New route: `projects/monolith/frontend/src/routes/public/app/firecracker/+page.svelte`,
  reachable at `jomcgi.dev/app/firecracker` via the existing subdomain reroute
  hook (`hooks.js` maps apex to `/public/*`).
- The page imports only baked static modules and makes **zero `/api/` fetches**,
  so it renders public-safe by construction (same property as the Grimoire
  landing).
- All styles scoped under a page class (e.g. `.fcstory`); nothing leaks into
  site-wide design tokens (lesson from the Grimoire reskin).
- Nav suppression: covered by the existing `/app/*` handling in the root layout.

## 2. Story choreography

Sticky full-viewport stage over a tall scroller. A single rAF loop maps document
scroll fraction to a master timeline `t`, passed to a pure `frame(t)` function in
a `timeline.js` module that imperatively mutates plain element refs.

Beats, in scroll order:

1. **Hook**: one sentence plus one real number ("this sandbox was ready in
   NNNms, here's how"), a timing bar frozen at the recorded value.
2. **Cold boot anatomy**: the stage assembles a microVM the slow way: kernel,
   rootfs, runtime init blocks stack up while a timing bar grows through the
   real recorded phase durations. Communicates the cost being avoided.
3. **Snapshot anatomy**: the booted VM freezes; its RAM visibly serializes into
   two files on a disk glyph (memory file + vmstate). A snapshot is just bytes
   on disk.
4. **Restore (centerpiece)**: scrubbing streams memory pages from the disk
   glyph back into a RAM grid; the VM wakes mid-thought; the restore timing bar
   completes at a fraction of the cold-boot bar shown alongside for contrast.
   Real recorded durations drive all proportions.
5. **Replay finale**: scroll settles into an interactive widget: a "restore
   again" button replays one of N baked, real recorded restores (each with its
   genuine jitter), a restore counter, and a per-phase breakdown. Repeatability
   is felt, not claimed, and costs the cluster nothing.

## 3. Bake pipeline

`bake-fc-story.sh` (colocated with the page's data dir), run privately against
the live cluster whenever we want fresh numbers:

1. Trigger one cold-boot run and ~10 snapshot restores through fc-invoke.
2. Query SigNoz ClickHouse (`signoz_traces`, same query pattern as the demos
   tracing work) for the span trees of those runs.
3. Emit a committed static `data/trace.js`: named phases with real millisecond
   durations for the cold run and each restore run.

Re-recording after a daemon improvement is: rerun script, review diff, commit.
The baked output contains only phase names and durations: no hostnames, IPs,
secrets, or infra topology.

## 4. Performance (fluidity requirement)

- Hot path mutates only compositor-friendly properties (transform, opacity);
  no layout-triggering reads or writes per frame.
- No Svelte reactive state on the per-frame path; runes only for coarse phase
  id and interactive flags. Per-frame geometry lives on plain objects.
- Choreography is authored and approved in a standalone HTML mockup first, so
  scrub feel is validated before the Svelte port.

## 5. Fallbacks and CI

- No-JS and `prefers-reduced-motion`: static stacked-scene fallback with the
  same real numbers (Grimoire pattern). Replay widget degrades to a static
  phase table.
- Add the page to `frontend/visual/targets.json` for visual regression (no API
  fetches, so no fixtures needed).
- Rollout: monolith chart bump(s) in the same PR (apex is served by the
  monolith public tier; both bumps as per the runbook).
- Read `docs/runbooks/public-tier-checklist.md` before implementation
  (public-tier change).

## 6. Process

Mockup-first workflow (as used for the Grimoire landing):

1. Build the full choreography as a self-contained HTML artifact Joe can scrub
   in a browser; iterate on motion and visual language there.
2. On mockup sign-off, port to Svelte and wire to baked data.
3. Implementation via writing-plans then subagent-driven execution; one
   comprehensive review at the PR boundary; CI on the pushed branch.
