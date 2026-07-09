# Public /app/firecracker Scroll Explainer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or the
> repo-default superpowers:subagent-driven-development) to implement this plan
> task-by-task. Task 3 is a HUMAN CHECKPOINT: Joe must approve the HTML mockup
> before any Svelte work starts.

**Goal:** A public `jomcgi.dev/app/firecracker` scroll-scrubbed explainer of the
fc-invoke daemon (cold boot vs snapshot restore, snapshot anatomy, replay
finale), driven by baked real trace data, with zero cluster load at request
time.

**Architecture:** Pattern-copy of the Grimoire ScrollStory (sticky full-viewport
stage, rAF scroll scrub, pure `frame(t)` timeline module, imperative DOM
mutation on the hot path, static no-JS/reduced-motion fallback). Real fc-invoke
span durations are recorded by a bake script through the existing private demos
API and committed as a static `data/trace.js` module. The page makes zero
`/api/` fetches. Design doc: `docs/plans/2026-07-08-firecracker-public-explainer-design.md`.

**Tech Stack:** SvelteKit (monolith frontend), bash + python3 bake script,
kubectl port-forward, existing demos API (`/api/demos/firecracker/*`), vitest
for the timeline module test.

**Reference implementations (read before each task):**

- Scroll story component: `projects/monolith/frontend/src/lib/public/grimoire/scrollstory/ScrollStory.svelte`
- Timeline module + test: `.../scrollstory/timeline.js`, `.../scrollstory/timeline.test.js`
- Bake script pattern: `.../scrollstory/bake-scrollstory.sh`
- Public landing route (zero-fetch pattern): `projects/monolith/frontend/src/routes/public/app/grimoire/+page.svelte`
- Choreography gotchas: `docs/plans/2026-07-06-grimoire-scroll-explainer.md` ("LOCKED CHOREOGRAPHY")
- Public tier gate: `docs/runbooks/public-tier-checklist.md`

**Non-negotiables (from design approval):**

- The scrub must roll fluidly: transform/opacity only on the per-frame path, no
  reactive state per frame, no layout reads/writes in `frame(t)`.
- Never write em-dashes in any copy, code comment, or commit message.
- Native browser scrolling only: no timed arrow-key glides, no mandatory
  scroll-snap (proximity snap + plateaus are fine). Joe rejected glides 3x on
  the grimoire story.
- Real numbers only: every duration shown comes from `data/trace.js`. No
  invented timings.
- Baked output contains only phase names + millisecond durations. No
  hostnames, IPs, node names, image refs, or infra topology.

---

### Task 1: Bake script and baked trace data

**Files:**

- Create: `projects/monolith/frontend/src/lib/public/fcstory/bake-fc-story.sh` (mode 755)
- Create (generated, committed): `projects/monolith/frontend/src/lib/public/fcstory/data/trace.js`

**Step 1: Write the bake script**

The script triggers real runs through the private demos API (in-cluster via
port-forward, so no Cloudflare Access involved) and captures each run's span
tree via the existing trace endpoint. It does NOT query ClickHouse directly:
`GET /api/demos/firecracker/trace/{trace_id}` already returns normalized spans
(see `projects/monolith/demos/firecracker_api.py:178`).

```bash
#!/usr/bin/env bash
# Bake real fc-invoke trace data for the public /app/firecracker explainer.
#
# Triggers real Python-sandbox runs through the private demos API, captures
# each run's span tree, and writes a static data/trace.js consumed by the
# public page. Re-run and commit data/ whenever the daemon improves.
#
# Usage: bake-fc-story.sh [runs]   (default 12)
# Requires: kubectl (cluster access), python3, curl, jq.
set -euo pipefail

RUNS="${1:-12}"
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/data"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; kill "${PF_PID:-}" 2>/dev/null || true' EXIT
mkdir -p "$OUT"

echo "▶ port-forwarding monolith backend"
kubectl port-forward -n monolith svc/monolith 18000:8000 >/dev/null 2>&1 &
PF_PID=$!
for i in $(seq 1 30); do
  curl -fsS -o /dev/null http://127.0.0.1:18000/healthz && break
  sleep 1
done

echo "▶ triggering $RUNS sandbox runs"
: >"$TMP/trace_ids"
for i in $(seq 1 "$RUNS"); do
  tid="$(curl -fsS -X POST http://127.0.0.1:18000/api/demos/firecracker/python \
    -H 'content-type: application/json' \
    -d '{"code": "print(\"baked for /app/firecracker\")"}' | jq -r .trace_id)"
  echo "  run $i: $tid"
  echo "$tid" >>"$TMP/trace_ids"
  sleep 2
done

echo "▶ waiting for SigNoz ingest, then fetching span trees"
sleep 20
: >"$TMP/spans.ndjson"
while read -r tid; do
  curl -fsS "http://127.0.0.1:18000/api/demos/firecracker/trace/$tid" \
    | jq -c --arg tid "$tid" '{trace_id: $tid, body: .}' >>"$TMP/spans.ndjson"
done <"$TMP/trace_ids"

echo "▶ writing data/trace.js"
python3 "$DIR/bake_trace.py" "$TMP/spans.ndjson" "$OUT/trace.js"
echo "✔ baked $OUT/trace.js"
```

Notes for the implementer:

- Check the actual request schema of `POST /api/demos/firecracker/python`
  (`PythonRequest` in `projects/monolith/demos/firecracker_api.py`) and the
  actual healthz path before assuming the ones above; adjust the script to
  match reality, not the plan.
- Check the trace endpoint's response shape (it wraps spans; see `get_trace`)
  and adjust the jq/python accordingly.

**Step 2: Write the span-to-phase normalizer**

Create `bake_trace.py` next to the script (it is bake-time tooling, not app
code; keep it out of any app bundle, same as the grimoire bake's inline
python). It must:

- Classify each run: **warm** if a `snapshot_restore` span is present, **cold**
  if `provision_rootfs` + `firecracker_boot` are present (also check the
  `fc.pool_hit` attribute if exposed).
- For each run emit ordered phases: `{name, ms}` using span names
  `auth_tokenreview`, `acquire_slot`, `snapshot_restore` OR
  `provision_rootfs` + `firecracker_boot`, `guest_wait_ready`, `guest_exec`.
  Exclude `guest_teardown` (post-response cleanup, not setup latency) and any
  span outside the fc_invoke subtree (the monolith/demo wrapper spans).
- Strip everything except phase names and durations (privacy rule above).
- Write `data/trace.js`:

```js
// GENERATED by bake-fc-story.sh - do not hand-edit.
// Regenerate: ./bake-fc-story.sh 12
export const cold = { total: 1234, phases: [{ name: "provision_rootfs", ms: 800 }, ...] };
export const restores = [
  { total: 240, phases: [{ name: "snapshot_restore", ms: 180 }, ...] },
  ...
];
export const bakedAt = "2026-07-08";
```

- If the pool never misses (zero cold runs among N), print a loud warning and
  exit non-zero with guidance (raise run count so the pool is exhausted, or
  temporarily scale the workload's pool in
  `projects/substrate/deploy/values.yaml`). Do NOT fabricate a cold run.

**Step 3: Run the bake against the live cluster**

Run: `./bake-fc-story.sh 12`
Expected: `data/trace.js` with 1+ cold run and ~10 restores; eyeball that
restore totals are plausibly a small fraction of the cold total and that no
infra strings leaked into the file.

**Step 4: Commit**

```bash
git add projects/monolith/frontend/src/lib/public/fcstory/
git commit -m "feat(fcstory): bake script and real fc-invoke trace data"
```

---

### Task 2: Static HTML mockup with the full choreography

**Files:**

- Create: `projects/monolith/frontend/src/lib/public/fcstory/reference-mockup.html`
  (self-contained: inline CSS/JS, the baked numbers from `data/trace.js`
  pasted in as a const; committed for provenance like the grimoire one)

**Step 1: Build the mockup**

A single HTML file implementing the entire scroll story with the real baked
numbers. Load the `artifact-design` skill and publish via the Artifact tool so
Joe can scrub it in a browser. Beats (from the design doc):

1. **Hook**: one sentence + the real restore total, frozen timing bar.
2. **Cold boot anatomy**: kernel/rootfs/runtime-init blocks stack while a
   timing bar grows through the real cold phase durations (proportional).
3. **Snapshot anatomy**: the VM freezes; RAM serializes into two files
   (memory file + vmstate) on a disk glyph.
4. **Restore (centerpiece)**: memory pages stream disk glyph -> RAM grid; the
   restore bar completes beside the full cold-boot bar for contrast.
5. **Replay finale**: interactive widget, "restore again" cycles the N baked
   restores with their genuine jitter, restore counter, per-phase breakdown.

Mechanics: tall scroller + sticky stage + rAF scrub mapping scroll fraction to
`t`, pure `frame(t)`. Apply the grimoire mockup gotchas up front:
`pointer-events: none` on invisible overlays, watch stacking contexts, document
needs `scroll-snap-type` for any snap alignment to do anything, and native
scrolling only.

**Step 2: HUMAN CHECKPOINT - iterate with Joe**

Publish the artifact, iterate rounds with Joe on motion, pacing, and visual
language until he approves (grimoire took ~9 rounds; expect several). Fluidity
is his one hard requirement: if the scrub hitches in the mockup, fix it here,
not in Svelte.

**Do not proceed to Task 3 without explicit approval.**

**Step 3: Commit the approved mockup**

```bash
git add projects/monolith/frontend/src/lib/public/fcstory/reference-mockup.html
git commit -m "feat(fcstory): approved reference mockup for the scroll story"
```

---

### Task 3: Svelte port - route, component, timeline module

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/app/firecracker/+page.svelte`
- Create: `projects/monolith/frontend/src/lib/public/fcstory/FcScrollStory.svelte`
- Create: `projects/monolith/frontend/src/lib/public/fcstory/timeline.js`
- Test: `projects/monolith/frontend/src/lib/public/fcstory/timeline.test.js`
- Possibly modify: nav suppression regex in
  `projects/monolith/frontend/src/routes/+layout.svelte` (check whether the
  existing `/app/*` handling already covers the new route before touching it)

**Step 1: Write the failing timeline test**

Port the choreography's phase math from the mockup into `timeline.js` (pure
module: easings, phase boundaries, functions from `t` to per-element geometry)
and write `timeline.test.js` first, mirroring the structure of the grimoire
`timeline.test.js`. Minimum assertions:

```js
import { describe, expect, it } from "vitest";
import { phases, barWidth } from "./timeline.js";
import { cold, restores } from "./data/trace.js";

describe("fcstory timeline", () => {
  it("phase boundaries are monotonically increasing and cover [0,1]", () => {
    // guards against a reordered beat silently overlapping another
  });
  it("timing bars are proportional to the baked durations", () => {
    // cold bar at t=1 spans the full cold.total; restore bar spans
    // restores[i].total / cold.total of it
  });
  it("baked data has at least one cold run and one restore", () => {
    expect(cold.phases.length).toBeGreaterThan(0);
    expect(restores.length).toBeGreaterThan(0);
  });
});
```

**Step 2: Port the mockup to Svelte**

`FcScrollStory.svelte`, translated 1:1 from the approved mockup:

- Sticky stage + tall scroller; single rAF loop; `frame(t)` from `timeline.js`
  mutates plain element refs (transform/opacity only). Svelte runes only for
  coarse phase id and interactive flags.
- All styles scoped under a `.fcstory` root class. Palette hex lives in a
  colocated `fcstory.css`, not in the `.svelte` `<style>` block (semgrep
  `svelte-hardcoded-color-in-style`).
- Static fallback: stacked scenes with the same real numbers, shown for no-JS
  and `prefers-reduced-motion` (grimoire pattern: interactive version CSS
  hidden until JS + motion allowed).
- `+page.svelte` imports only `FcScrollStory` and baked data. Zero fetches.

**Step 3: Verify locally (hot-reload loop, NOT CI round-trips)**

```bash
cd /tmp/claude-worktrees/fc-public-explainer/projects/monolith/frontend
pnpm install && pnpm dev
# open http://localhost:5202/public/app/firecracker  (localhost is not the
# apex host, so use the raw /public prefix)
```

Scrub end to end; compare against the mockup. Screenshot at exact timeline
fractions with the Playwright helper in `frontend/visual` if pixel comparison
is needed. If a local `pnpm build` is ever run: `rm -rf build && git checkout
-- projects/monolith/frontend/BUILD` (macOS case-insensitive FS clobbers the
Bazel BUILD file).

**Step 4: Commit**

```bash
git add projects/monolith/frontend/src
git commit -m "feat(fcstory): public /app/firecracker scroll explainer (svelte port)"
```

---

### Task 4: Replay finale widget

**Files:**

- Create: `projects/monolith/frontend/src/lib/public/fcstory/ReplayWidget.svelte`
- Modify: `FcScrollStory.svelte` (mount it as the final beat)

**Step 1: Implement**

Client-side only, driven entirely by `restores` from `data/trace.js`:

- "Restore again" button replays the next recorded restore run: phase bar
  animates through that run's real per-phase durations in real time (a 240ms
  restore animates for 240ms; it should feel startlingly fast).
- Restore counter increments; per-phase breakdown shown for the last run;
  cycles through the N baked runs so jitter is visibly real.
- `prefers-reduced-motion`: no animation, static per-phase table + counter.

**Step 2: Verify in the dev server, commit**

```bash
git add projects/monolith/frontend/src/lib/public/fcstory
git commit -m "feat(fcstory): replay finale widget cycling baked restores"
```

---

### Task 5: Visual regression coverage

**Files:**

- Modify: `projects/monolith/frontend/visual/targets.json`

**Step 1: Add the page**

```json
{ "id": "firecracker", "path": "/app/firecracker" }
```

No API fixtures needed (zero-fetch page). Check how other scroll pages are
captured; if the grimoire landing uses `static_initial` or a scroll position
knob, mirror it so the capture is deterministic.

**Step 2: Commit**

```bash
git add projects/monolith/frontend/visual/targets.json
git commit -m "test(fcstory): visual regression target for /app/firecracker"
```

---

### Task 6: Chart bumps, PR, CI, rollout verification

**Step 1: Public-tier gate check**

Re-read `docs/runbooks/public-tier-checklist.md`. For this page: no DB reads
(no grants), no `/api` calls (no proxy route), but confirm the fcstory lib path
is included in the public binary's frontend build (item 3 analog) and that
BOTH charts get bumped.

**Step 2: Bump both charts**

```bash
bazel/tools/git/bump-chart.sh projects/monolith
# plus the chart serving the apex:
bazel/tools/git/bump-chart.sh projects/<monolith-public service dir>  # locate it: grep -rl monolith-public projects/*/deploy
git add -A && git commit -m "chore(fcstory): bump monolith and monolith-public charts"
```

**Step 3: Format, push, PR, watch CI**

```bash
bazel/tools/format/fast-format.sh
git push -u origin feat/fc-public-explainer
gh pr create --title "feat: public /app/firecracker scroll explainer" --body "..."
gh pr checks <number> --watch
```

One comprehensive code review at the PR boundary (repo policy), then
`gh pr merge --rebase` on green.

**Step 4: Post-deploy verification (the only one that counts)**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://jomcgi.dev/app/firecracker
```

Expect 200 from the live apex. Beware the pod-rollout race: ArgoCD Healthy can
fire while the terminating pod still serves; re-curl after the rollout settles
and confirm the page body contains the baked restore number, not just a 200.
