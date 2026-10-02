// Pure view-model for the LLM leaderboard page: ranking, the model presets the
// selector offers, and the per-metric readings the charts and table share. The
// page holds no logic of its own beyond wiring these to the selection.

// Fixed provider order for the categorical palette (leaderboard.css owns the
// colours). A provider keeps its slot whatever is selected, so filtering never
// repaints the survivors. Anything past the eighth slot folds into "other".
export const PROVIDERS = [
  "qwen",
  "anthropic",
  "google",
  "deepseek",
  "z-ai",
  "mistralai",
  "tencent",
  "cohere",
];

export function provider(id) {
  return String(id).split("/")[0];
}

export function providerSlot(id) {
  const index = PROVIDERS.indexOf(provider(id));
  return index === -1 ? "other" : String(index + 1);
}

export function shortName(model) {
  if (model.name) return model.name;
  const id = String(model.id);
  return id.includes("/") ? id.split("/").slice(1).join("/") : id;
}

export const hardRate = (m) => (m.hard_n ? m.hard_pass / m.hard_n : 0);

// Rank by hard-task pass RATE, not count: newer models ran more hard tasks, and
// a raw count would rank them above a model that solved everything it was given.
// Then cost per solve, then wall-time, so the cheaper of two equals leads.
export function rank(models) {
  return [...models].sort(
    (a, b) =>
      Number(b.qualified) - Number(a.qualified) ||
      hardRate(b) - hardRate(a) ||
      (a.cost_per_solve_usd ?? a.cost_usd ?? 0) -
        (b.cost_per_solve_usd ?? b.cost_usd ?? 0) ||
      (a.mean_latency_ms ?? 0) - (b.mean_latency_ms ?? 0),
  );
}

// Presets. The default is deliberately not everything: the Claude anchors as the
// ceiling, the self-hosted rows we actually run, and the best few rented models.
const TOP_CLOUD = 4;

export const PRESETS = [
  { key: "default", label: "Top picks" },
  { key: "self", label: "Self-hosted" },
  { key: "perfect", label: "All hard solved" },
  { key: "all", label: "All" },
];

export function presetIds(models, key) {
  const ranked = rank(models);
  const anchors = ranked.filter((m) => m.role === "anchor");
  const local = ranked.filter((m) => m.self_hosted);
  let picked;
  if (key === "all") picked = ranked;
  else if (key === "self") picked = [...local, ...anchors];
  else if (key === "perfect")
    picked = ranked.filter((m) => m.qualified && hardRate(m) >= 1);
  else {
    const cloud = ranked
      .filter((m) => m.role !== "anchor" && !m.self_hosted && m.qualified)
      .slice(0, TOP_CLOUD);
    picked = [...anchors, ...local, ...cloud];
  }
  return new Set(picked.map((m) => m.id));
}

// The preset a selection matches, or null for a hand-picked one.
export function matchPreset(models, ids) {
  for (const preset of PRESETS) {
    const want = presetIds(models, preset.key);
    if (want.size === ids.size && [...want].every((id) => ids.has(id)))
      return preset.key;
  }
  return null;
}

// Selection survives in the URL as ?m=<id>,<id> so a view can be linked. Unknown
// ids are dropped; an empty or absent list means the default preset.
export function parseSelection(models, raw) {
  const known = new Set(models.map((m) => m.id));
  const ids = String(raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter((id) => known.has(id));
  return ids.length ? new Set(ids) : presetIds(models, "default");
}

export function serializeSelection(models, ids) {
  if (matchPreset(models, ids) === "default") return null;
  return rank(models)
    .filter((m) => ids.has(m.id))
    .map((m) => m.id)
    .join(",");
}

export function money(v) {
  if (v == null) return "n/a";
  if (v === 0) return "$0";
  if (v < 0.01) return `$${v.toFixed(4)}`;
  if (v >= 1) return `$${v.toFixed(2)}`;
  return `$${v.toFixed(3)}`;
}

export function secs(v) {
  if (v == null) return "n/a";
  return v < 10 ? `${v.toFixed(1)}s` : `${Math.round(v)}s`;
}

export function kfmt(v) {
  if (v == null) return "n/a";
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10_000 ? 0 : 1)}k`;
  return String(Math.round(v));
}

export const pct = (v) => `${Math.round(v * 100)}%`;

// One reading per comparison, shared by the bar charts, the scatter tabs and
// the table. `better` says which end wins so each chart sorts best-first.
export const METRICS = {
  hard: {
    label: "Hard-task pass",
    unit: "share of hard tasks solved",
    better: "higher",
    get: hardRate,
    fmt: pct,
    note: (m) => `${m.hard_pass}/${m.hard_n}`,
  },
  cost: {
    label: "Cost per task",
    unit: "mean list price per task",
    better: "lower",
    get: (m) => m.cost_usd,
    fmt: money,
    tick: (v) => `$${Number(v.toPrecision(2))}`,
    note: (m) => (m.self_hosted ? "own GPU" : ""),
    log: true,
  },
  wall: {
    label: "Wall-time per task",
    unit: "mean seconds per task",
    better: "lower",
    get: (m) => (m.mean_latency_ms ?? 0) / 1000,
    fmt: secs,
    tick: (v) => `${Math.round(v)}s`,
    note: (m) => (m.self_hosted ? "one 4090" : ""),
  },
  tokens: {
    label: "Tokens per task",
    unit: "mean prompt + completion tokens per task",
    better: "lower",
    get: (m) => m.mean_tokens,
    fmt: kfmt,
    note: () => "",
    log: true,
    // Claude runs inside Claude Code's own harness, whose tokens and turns are
    // not counted the same way, so the anchors sit out of these two metrics.
    candidatesOnly: true,
  },
  turns: {
    label: "Agent steps per task",
    unit: "mean tool-calling turns per task",
    better: "lower",
    get: (m) => m.mean_turns,
    fmt: (v) => (v == null ? "n/a" : v.toFixed(1)),
    note: () => "",
    candidatesOnly: true,
  },
};

// The models a metric can honestly plot.
export function plottable(models, key) {
  return METRICS[key].candidatesOnly
    ? models.filter((m) => m.role !== "anchor")
    : [...models];
}

// Pareto frontier of {x, y} points where lower x and higher y are better: a
// point is on it when no other point has at least its y for at most its x,
// with one of the two strictly better. Returned left to right.
export function paretoFrontier(points) {
  return points
    .filter(
      (p) =>
        !points.some(
          (q) =>
            q !== p && q.y >= p.y && q.x <= p.x && (q.y > p.y || q.x < p.x),
        ),
    )
    .sort((a, b) => a.x - b.x || a.y - b.y);
}

// Providers present in a selection, in palette order.
export function providersIn(models) {
  const rank = (p) => {
    const i = PROVIDERS.indexOf(p);
    return i === -1 ? PROVIDERS.length : i;
  };
  return [...new Set(models.map((m) => provider(m.id)))].sort(
    (a, b) => rank(a) - rank(b),
  );
}

export function sortByMetric(models, key) {
  const metric = METRICS[key];
  const sign = metric.better === "higher" ? -1 : 1;
  return plottable(models, key).sort(
    (a, b) => sign * ((metric.get(a) ?? 0) - (metric.get(b) ?? 0)),
  );
}

// Per-task state for the matrix: a cell the model never ran is "none", a cell
// that failed before grading is "errored" (it does not count against the
// model), otherwise pass or fail.
export function cellState(model, taskId) {
  if ((model.errored_tasks ?? []).includes(taskId)) return "errored";
  const cell = (model.tasks ?? []).find((t) => t.id === taskId);
  if (!cell) return "none";
  return cell.passed ? "pass" : "fail";
}

const TIER_ORDER = { easy: 0, standard: 1, hard: 2 };

// Tasks in reading order for the matrix and the key: easy, standard, hard, then
// by id, numbered so the matrix columns can be keyed rather than labelled.
export function orderedTasks(tasks) {
  return [...tasks]
    .sort(
      (a, b) =>
        (TIER_ORDER[a.tier] ?? 1) - (TIER_ORDER[b.tier] ?? 1) ||
        a.id.localeCompare(b.id),
    )
    .map((task, index) => ({ ...task, no: index + 1 }));
}

export function fmtDate(iso) {
  if (!iso) return "n/a";
  const date = new Date(`${iso}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return "n/a";
  return date.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}
