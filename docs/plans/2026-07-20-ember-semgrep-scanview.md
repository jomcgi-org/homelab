# Ember Semgrep ScanView Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild /ember/semgrep around the converged journey mock: findings ignite in the code as a sweep passes, then a cold-vs-snapshot race with measured numbers; copy in the cv register; Semgrep credited and linked.

**Architecture:** The interactive mock at `docs/plans/2026-07-20-ember-semgrep-scanview-mock.html` IS the visual and behavioral spec (open it in a browser; everything inside the white panel is the page). A new `ScanView.svelte` component owns editor, gutter, sweep, finding pins, receipt line, and race; `+page.svelte` composes it with the existing session/Turnstile shell and proxies (unchanged). Backend changes are limited to the baseline constant's semantics.

**Key measured facts (do not re-derive):**
- Cold start = what the daemon pays building the warm base at startup: boot VM + start engine + load 1,600 rules to ready. Measured on node-4, 2026-07-20 daemon log: `warm base built key=semgrep took=6.85s`. Constant: `COLD_START_MS = 6_850`.
- Race semantics: warm lane = actual `scan_ms`. Cold lane total = `cold_start_ms + scan_ms` (computed frontend-side from the response). Per-scan savings = exactly `COLD_START_MS` (the skipped cold start).
- Warm restore ~21 ms (documented FC snapshot-restore timings on this hardware).

**Copy is FINAL, ship these strings verbatim (cv register, no em-dashes, no consequence narration, no "X, not Y"):**
- Lede: "A Semgrep Pro engine loaded its 1,600 rules once, then was frozen as a microVM snapshot. Every scan thaws its own fresh copy in 21 ms." (Semgrep Pro and 21 ms bolded per mock.)
- Topbar right: "{N} snippets scanned · {M} min of cold starts skipped" (hide a segment while its value is null).
- During scan: "microVM restored · 21 ms" (dim). Receipt: "{n} finding(s) · scanned in {x.xx} s".
- Race title: "the same scan, cold". Lane labels: "warm restore / rules already in memory" and "cold start / boot VM · load 1,600 rules · scan". Cold lane segment labels while crossing: "booting…", "loading rules…", "scanning…". Countdown stat: "{y.y} s to go", end stats "done · {t} s". NO verdict sentence after the race.
- Empty state (no findings): "clean. every example above hides something it catches."
- Pre-scan results hint: "findings land on their lines as the scan passes them."
- Footer: "each scan runs in its own microVM, destroyed after the response · engine: [Semgrep Pro](https://semgrep.dev) · snapshots: [Firecracker](/ember/firecracker)".

---

### Task 1: Backend baseline re-semantics

**Files:**
- Modify: `projects/monolith/ember_public/semgrep_core.py`
- Modify: `projects/monolith/ember_public/semgrep_router.py`
- Modify: `projects/monolith/ember_public/semgrep_core_test.py`, `semgrep_router_test.py`
- Create: migration in `projects/monolith/chart/migrations/` (next sequence number)

**Steps:**
1. Replace `HOSTED_SCAN_MEDIAN_MS = 11_000` with `COLD_START_MS = 6_850` and a comment citing the measurement (daemon log `warm base built key=semgrep took=6.85s`, node-4, 2026-07-20). Grep the test tree for `HOSTED_SCAN_MEDIAN_MS` and `11_000`/`11000` and update in the same commit (repo rule).
2. `saved_ms(scan_ms)` becomes `saved_ms() -> int: return COLD_START_MS` (per-scan saving is the skipped cold start; scan time is paid on both paths). Update accrual call site and tests.
3. Router response: replace `baseline_ms` with `cold_start_ms: COLD_START_MS`. Keep `scan_ms`, `queued_ms`, `saved_ms` as-is.
4. Migration: `UPDATE demo_sg_savings SET scans = 0, actual_ms = 0, saved_ms = 0;` (semantics changed; the row holds only test scans). No grants needed (UPDATE on existing table runs as the migration role).
5. py_compile, format (revert the known gazelle/doc-manifest drift outside intended files), commit: `feat(ember): re-baseline semgrep demo savings to measured cold start`.

### Task 2: ScanView component + page rebuild

**Files:**
- Create: `projects/monolith/frontend/src/lib/public/ember/ScanView.svelte`
- Rewrite: `projects/monolith/frontend/src/routes/public/ember/semgrep/+page.svelte`
- Reference (read first, in a browser if possible): `docs/plans/2026-07-20-ember-semgrep-scanview-mock.html`, including its `<script>` choreography (sweep frame loop, ignition thresholds, race rAF loop, reduced-motion branches). Port that logic to Svelte 5 runes; timings and easings match the mock.

**Spec (mock is authoritative; deltas and bindings):**
- ScanView props/events: examples list, language, code (bindable), scanning state; emits scan requests; receives the scan response and drives the choreography. Session/Turnstile gating, proxies, rate/queue/error handling stay in `+page.svelte` exactly as today (the busy/queued UI states from the current page are kept, restyled to fit).
- Editor: keep the real `<textarea>` (users must edit), with the mock's gutter/row visual built as an overlay or mirrored render. The findings ignition needs per-line row rendering: use a mirrored `<pre>` behind/beside the textarea sharing font metrics (the current page already syncs a gutter; extend that approach). Finding flags pin right of the line per mock; clicking a finding row in the detail list highlights the line (keep from current page).
- Sweep: duration `max(scan_ms, 700)` ms replayed AFTER the response arrives (we do not know findings until then; the whole choreography plays post-response: restore line, sweep, ignitions, receipt). During the network wait show the "microVM restored · 21 ms" dim line and the button's "scanning" state; if wall time exceeds ~1.5 s show the queued narration (existing behavior).
- Race: starts 700 ms after receipt, runs once per scan, cold lane duration `cold_start_ms + scan_ms` real-time with countdown; segment fractions: booting 0 to 0.25, loading rules 0.25 to 0.93, scanning 0.93 to 1 (illustrative split within a measured total; note this in a code comment). Reduced motion: all end states snap, no loops.
- Examples as chips per mock (two javascript first, then python's two; the "python ›" chip pattern from the mock becomes plain per-example chips grouped by language with the js pair first). Language derives from the selected example; free editing keeps the current language.
- Topbar counters from savings (SSR seed + refresh after scan). Footer per final copy. Landing door card description on /ember stays as-is.
- Style: all hex via `--em-*` tokens or the colocated css file; no prop named `state`; no new npm deps; `svelte-hardcoded-color-in-style` applies.
- Tests: keep/adapt the session proxy vitest; visual regression will re-baseline in CI (a NEW look is an intentional diff; check the CI visual job output rather than assuming failure = bug).
- Commit: `feat(ember): rebuild semgrep demo around ScanView journey`.

### Task 3: Bumps, PR, review, merge, live verify

1. `bazel/tools/git/bump-chart.sh projects/monolith` (couples monolith-public). Commit `chore(monolith,monolith-public): bump charts for semgrep scanview`.
2. Push, PR, one comprehensive Opus review of the full diff (check the choreography logic against the mock's script, the copy strings verbatim, reduced-motion paths, and that no vendor product is named). CI via `gh pr checks --watch`; auto-merge on green; re-bump on version collision if BEHIND/DIRTY.
3. Post-deploy: curl 200, in-pod scan (fabricated `demo_sg_session` cookie bypasses Turnstile; recipe in project memory), confirm response carries `cold_start_ms` and savings accrue at `COLD_START_MS` per scan. Live screenshot of the full journey for the record.
