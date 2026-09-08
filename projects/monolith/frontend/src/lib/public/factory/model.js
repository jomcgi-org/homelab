import { last14, window } from "./charts.js";

const MODEL_LANES = new Map([
  ["luna", "luna"],
  ["terra", "codex"],
  ["sol", "codex"],
  ["opus", "claude"],
  ["sonnet", "claude"],
  ["fable", "claude"],
  ["spark", "spark"],
  ["pi-spark", "spark"],
]);

const TYPE_KEYS = ["feat", "fix", "docs", "chore", "test", "refactor", "other"];

const numeric = (value) => Number(value ?? 0);

export function formatSpend(value) {
  const rounded = Math.round(numeric(value));
  const abbreviated = (amount, suffix) =>
    `${amount.toFixed(1).replace(/\.0$/, "")}${suffix}`;
  if (rounded >= 1e6) return `$${abbreviated(rounded / 1e6, "M")}`;
  if (rounded >= 1e3) return `$${abbreviated(rounded / 1e3, "k")}`;
  return `$${rounded}`;
}

/**
 * @param {string | null} modelName
 * @returns {"luna" | "codex" | "claude" | "spark" | "other"}
 */
export function modelLane(modelName) {
  if (typeof modelName !== "string") return "other";
  const model = modelName.toLowerCase();
  if (model.startsWith("claude-")) return "claude";
  if (model === "gpt-5.6-luna") return "luna";
  if (
    model === "gpt-5.6-terra" ||
    model === "gpt-5.6-sol" ||
    model === "codex-auto-review"
  )
    return "codex";
  if (model === "muse" || model.startsWith("muse-") || model.includes("spark"))
    return "spark";
  return MODEL_LANES.get(model) ?? "other";
}

export function activitySeries(rows) {
  const byDay = new Map();
  for (const row of rows) {
    const value = byDay.get(row.day) ?? {
      d: row.day,
      luna: 0,
      codex: 0,
      claude: 0,
      spark: 0,
      other: 0,
      sessions: 0,
      input_tokens: 0,
      output_tokens: 0,
    };
    value[modelLane(row.model)] += numeric(row.sessions);
    value.sessions += numeric(row.sessions);
    value.input_tokens += numeric(row.input_tokens);
    value.output_tokens += numeric(row.output_tokens);
    byDay.set(row.day, value);
  }
  return [...byDay.values()].sort((a, b) => a.d.localeCompare(b.d));
}

export function spendSeries(rows) {
  const byDay = new Map();
  for (const row of rows) {
    const value = byDay.get(row.day) ?? { d: row.day, spend_usd: 0 };
    value.spend_usd += numeric(row.spend_usd);
    byDay.set(row.day, value);
  }
  return [...byDay.values()].sort((a, b) => a.d.localeCompare(b.d));
}

export function mergeSeries(rows) {
  return rows.map((row) => ({
    ...row,
    n: TYPE_KEYS.reduce((sum, key) => sum + numeric(row[key]), 0),
    rest:
      numeric(row.chore) +
      numeric(row.test) +
      numeric(row.refactor) +
      numeric(row.other),
  }));
}

export function lineSeries(rows) {
  const byDay = new Map();
  for (const row of rows) {
    const d = row.merged_at?.slice(0, 10);
    if (!d) continue;
    const value = byDay.get(d) ?? { d, add: 0, del: 0 };
    value.add += numeric(row.additions);
    value.del += numeric(row.deletions);
    byDay.set(d, value);
  }
  return [...byDay.values()].sort((a, b) => a.d.localeCompare(b.d));
}

export function factSeries(facts, now) {
  return window(facts.daily ?? [], now, 30).map((row) => ({
    ...row,
    v: numeric(row.verified),
    u: numeric(row.unverified),
    n: numeric(row.verified) + numeric(row.unverified),
  }));
}

export function sortPullRequests(rows, sort, direction) {
  const key =
    {
      date: (row) => row.merged_at,
      type: (row) => row.type,
      area: (row) => row.scope || "~",
      lines: (row) => numeric(row.additions) + numeric(row.deletions),
    }[sort] ?? ((row) => row.merged_at);
  return [...rows].sort((a, b) => {
    const av = key(a);
    const bv = key(b);
    return (
      (av < bv ? -1 : av > bv ? 1 : 0) * direction ||
      String(b.merged_at).localeCompare(String(a.merged_at))
    );
  });
}

export function paginate(rows, requestedPage, pageSize) {
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const page = Math.min(Math.max(0, requestedPage), pageCount - 1);
  const start = rows.length ? page * pageSize + 1 : 0;
  const end = Math.min(rows.length, (page + 1) * pageSize);
  return {
    rows: rows.slice(page * pageSize, (page + 1) * pageSize),
    page,
    pageCount,
    start,
    end,
  };
}

export function breakdown(rows, value, limit = 4) {
  const counts = new Map();
  for (const row of rows) {
    const key = value(row) || "other";
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit);
}

export function tileDerivations(activity, merges, facts, series, now) {
  const add = numeric(merges.totals?.add_7d);
  const del = numeric(merges.totals?.del_7d);
  const latestFactDay = (facts.daily ?? [])
    .map((row) => row.d)
    .sort()
    .at(-1);
  const totals = activity.totals_7d ?? {};
  const ember = totals.ember ?? totals;
  const local = totals.local ?? {};
  const combined = totals.combined ?? totals;
  return {
    live: {
      value: numeric(activity.now?.active_last_hour),
      sessionsToday: numeric(activity.now?.sessions_today),
      spark: last14(series.sessions, "sessions", now),
    },
    sessions: {
      value: numeric(combined.sessions),
      ember: numeric(ember.sessions),
      local: numeric(local.sessions),
      spark: last14(series.sessions, "sessions", now),
    },
    merged: {
      value: numeric(merges.totals?.n_7d),
      agent: numeric(merges.totals?.agent_7d),
      spark: last14(series.merges, "n", now),
    },
    lines: {
      additions: add,
      deletions: del,
      net: add - del,
      spark: last14(series.lines, "add", now),
    },
    tokens: {
      input: numeric(combined.input_tokens),
      output: numeric(combined.output_tokens),
      spark: last14(series.sessions, "input_tokens", now),
    },
    spend: {
      value: numeric(combined.spend_usd),
      spark: last14(series.spend, "spend_usd", now),
    },
    facts: {
      value:
        numeric(facts.totals?.verified) + numeric(facts.totals?.unverified),
      verified: numeric(facts.totals?.verified),
      latestDay: latestFactDay,
      spark: last14(series.facts, "n", now),
    },
  };
}

export function markClass(note) {
  if (note.verification_state === "invalidated") return "invalidated";
  if (note.verification_state === "disputed" || note.disputed)
    return "disputed";
  return note.verification_state === "verified" ? "verified" : "unverified";
}

export function cleanPullTitle(title) {
  return title.replace(/^\w+(\(.*?\))?!?:\s*/, "");
}
