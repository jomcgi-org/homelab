// Single source of truth for the Firecracker timing figures quoted across
// the public site (homepage, engineering deep-dive, CV, diagrams). Every
// site-copy ms literal about sandbox restore or agent cold start should
// import from here instead of hardcoding a number, so a re-bake of
// data/trace.js (see bake-fc-story.sh) or a re-measurement of the agent
// platform numbers only ever needs one edit.
//
// Two families of number live here:
//   - sandbox* / cold*: derived from data/trace.js, the real baked trace
//     data behind the /ember/firecracker explainer (see FcScrollStory.svelte,
//     which imports the same derivation from this module).
//   - agent*: the agent-platform cold-start figures. trace.js only covers
//     the sandbox demo daemon, not the full agent-platform request path
//     (controller dispatch, egress proxy, model call), so these come from
//     separate measured agent-platform runs and are not re-derivable from
//     trace.js. Re-bake by hand when the agent platform is re-measured.

import { cold, restores } from "./data/trace.js";

// Mean snapshot_restore duration across every baked warm-restore run
// (22.46ms over 12 runs at last bake), the number FcScrollStory's hero and
// static fallback both use for "every sandbox since". This is the number
// site copy means by "sandbox restore" or "FC restore".
export const sandboxRestoreMs = Math.round(
  restores.reduce(
    (sum, r) => sum + r.phases.find((p) => p.name === "snapshot_restore").ms,
    0,
  ) / restores.length,
);

// Cold, once at daemon startup: the wait for a useful guest (kernel up,
// agent listening, toolchain warm), the dominant term in the 9.3s cold
// build.
export const coldGuestWaitMs = Math.round(
  cold.phases.find((p) => p.name === "guest_wait_ready").ms,
);

// Full cold base-snapshot build, seconds, one decimal (matches the hero's
// "X seconds" phrasing).
export const baseSnapshotBuildSec = Math.round(cold.total / 100) / 10;

// Number of real recorded runs baked into the explainer and replay widget.
export const runCount = restores.length;

// ---- Agent-platform figures (measured agent-platform runs, not derived
// from trace.js; re-bake by hand when the agent platform is re-measured) ----

// Warm restore of an idle agent thread's snapshot (memory + rootfs).
export const agentRestoreWarmMs = 6;

// Cold restore of an idle agent thread's snapshot.
export const agentRestoreColdMs = 28;

// Trigger to first model call, end to end, for the agent platform (not the
// sandbox demo): dispatch + microVM restore + guest init + agent bring-up.
export const agentFirstModelCallMs = 140;

// ---- Semgrep scan-guest figures (measured fc-invoke semgrep workload runs,
// visible live on the /ember/firecracker demos Semgrep tab: snapshot_restore
// ~21.9ms, ~0.72s wall for a single Pro taint scan vs a ~6.7s cold start.
// Not derivable from trace.js, which only covers the sandbox demo daemon;
// re-bake by hand when the semgrep workload is re-measured) ----
export const semgrepRestoreMs = 22;
export const semgrepScanSec = 0.72;
export const semgrepColdStartSec = 6.7;
