// Pure view-model helpers for the drain console page. The server composes
// state words and evidence; this module only formats and classifies for
// rendering, so it stays testable without mounting the page.

// A healthy drain job answers in single-digit tool calls (8 and 12 measured
// with thinking on); the degenerate loop measured 434 and 461. The line
// between them is drawn with an order of magnitude of slack on the healthy
// side so a legitimately busy job does not get painted as a runaway.
export const RUNAWAY_CALLS = 100;

// Lane state to the console's state-token vocabulary (agents-theme.css):
// ok is running, attn is degraded or waiting on time, err is broken, and
// idle/muted carries the no-news states.
const LANE_CLASSES = {
  running: "ok",
  quiet: "attn",
  wedged: "err",
  stranded: "err",
  waiting: "attn",
  idle: "idle",
  off: "idle",
  unknown: "attn",
};

const JOB_CLASSES = {
  running: "ok",
  due: "attn",
  scheduled: "idle",
  ok: "idle",
  error: "err",
  parked: "idle",
};

export function laneClass(state) {
  return LANE_CLASSES[state] || "idle";
}

export function jobClass(state) {
  return JOB_CLASSES[state] || "idle";
}

// The client re-derives ages every second against the SERVER clock: the
// payload carries `now` so a skewed workstation clock cannot turn a healthy
// 6s checkpoint into a phantom wedge (or hide a real one).
export function clockOffsetMs(serverNowIso, clientNowMs = Date.now()) {
  const server = Date.parse(serverNowIso || "");
  if (Number.isNaN(server)) return 0;
  return server - clientNowMs;
}

export function ageSeconds(iso, offsetMs = 0, nowMs = Date.now()) {
  const then = Date.parse(iso || "");
  if (Number.isNaN(then)) return null;
  return Math.max(0, Math.floor((nowMs + offsetMs - then) / 1000));
}

// Ticking clock for the rail: precise below an hour because the whole point
// is watching the number move (or not move).
export function fmtClockAge(seconds) {
  if (seconds == null) return "";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60)
    return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function fmtDuration(seconds) {
  if (seconds == null) return "";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

export function filterJobs(jobs, filter) {
  if (!filter || filter === "all") return jobs || [];
  return (jobs || []).filter((job) => job.state === filter);
}

// Effective call count for the fingerprint: a live turn reports its partial
// activity count, a finished one its recorded total.
export function jobCalls(job) {
  const live = job?.session?.live_calls;
  if (job?.state === "running" && typeof live === "number") return live;
  const calls = job?.session?.calls;
  return typeof calls === "number" ? calls : null;
}

// Log scale so 8 and 434 both render inside one small track while staying
// obviously different; linear would pin every healthy job at zero width.
export function fingerprintFraction(calls) {
  if (calls == null || calls <= 0) return 0;
  return Math.min(1, Math.log10(1 + calls) / Math.log10(1 + 500));
}

export function isRunaway(calls) {
  return typeof calls === "number" && calls >= RUNAWAY_CALLS;
}

// Mirrors activityParts in Turns.svelte: activities arrive in several
// historical shapes, and each renders as "verb detail".
export function activityLine(activity) {
  if (typeof activity === "string") return activity;
  if (!activity || typeof activity !== "object") return String(activity ?? "");
  const kind = String(activity.type || activity.tool || activity.name || "");
  const detail =
    activity.command ||
    activity.file_path ||
    activity.path ||
    compactInput(activity.input);
  return detail ? `${kind} ${detail}`.trim() : kind;
}

function compactInput(input) {
  if (input == null) return "";
  if (typeof input === "string") return input;
  try {
    return JSON.stringify(input);
  } catch {
    return "";
  }
}
