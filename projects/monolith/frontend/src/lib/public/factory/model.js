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

export function shortNumber(value) {
  const n = numeric(value);
  if (n >= 1e12) return `${(n / 1e12).toFixed(1)}T`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}k`;
  return String(n);
}

export function formatCount(value) {
  return numeric(value).toLocaleString();
}

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
  const totals = activity.totals_7d ?? {};
  const combined = totals.combined ?? totals;
  return {
    live: {
      value: numeric(activity.now?.active_last_hour),
      sessionsToday: numeric(activity.now?.sessions_today),
      spark: last14(series.sessions, "sessions", now),
    },
    sessions: {
      value: numeric(combined.sessions),
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

export function goalSummary(week, now, { windowHours = 72, limit = 3 } = {}) {
  const empty = { windowHours, total: 0, goals: [] };
  if (!Array.isArray(week) || week.length === 0) return empty;

  const nowMs = new Date(now).getTime();
  if (!Number.isFinite(nowMs)) return empty;
  const startMs = nowMs - windowHours * 60 * 60 * 1000;
  const areas = new Map();
  let total = 0;

  for (const row of week) {
    const mergedMs = new Date(row.merged_at).getTime();
    if (!Number.isFinite(mergedMs) || mergedMs < startMs || mergedMs > nowMs)
      continue;

    const area =
      typeof row.scope === "string" && row.scope.trim()
        ? row.scope.trim()
        : "unscoped";
    const type =
      typeof row.type === "string" && row.type.trim()
        ? row.type.trim()
        : "other";
    const value = areas.get(area) ?? {
      area,
      merged: 0,
      typeCounts: new Map(),
      additions: 0,
      deletions: 0,
      recent: [],
    };
    value.merged += 1;
    value.typeCounts.set(type, (value.typeCounts.get(type) ?? 0) + 1);
    value.additions += numeric(row.additions);
    value.deletions += numeric(row.deletions);
    value.recent.push({
      number: row.number,
      title: cleanPullTitle(String(row.title ?? "")),
      mergedMs,
    });
    areas.set(area, value);
    total += 1;
  }

  const goals = [...areas.values()]
    .sort((a, b) => b.merged - a.merged || a.area.localeCompare(b.area))
    .slice(0, limit)
    .map((area) => {
      const types = [...area.typeCounts].sort(
        (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
      );
      const [dominantType, dominantCount] = types[0] ?? ["other", 0];
      const dominantShare = dominantCount / area.merged;
      const buildAndFixShare =
        ((area.typeCounts.get("fix") ?? 0) +
          (area.typeCounts.get("feat") ?? 0)) /
        area.merged;
      let focus = "mixed work";
      if (dominantShare >= 0.6) {
        focus =
          {
            fix: "hardening",
            feat: "building",
            docs: "documenting",
          }[dominantType] ?? "maintaining";
      } else if (buildAndFixShare >= 0.6) {
        focus = "building and hardening";
      }

      return {
        area: area.area,
        merged: area.merged,
        share: area.merged / total,
        focus,
        types,
        additions: area.additions,
        deletions: area.deletions,
        recent: area.recent
          .sort(
            (a, b) =>
              b.mergedMs - a.mergedMs || numeric(b.number) - numeric(a.number),
          )
          .slice(0, 2)
          .map(({ number, title }) => ({ number, title })),
      };
    });

  return { windowHours, total, goals };
}

export function snapshotFreshness(
  snapshottedAt,
  now,
  { staleAfterMinutes = 90 } = {},
) {
  const iso = typeof snapshottedAt === "string" ? snapshottedAt : null;
  const snapshot = iso ? new Date(iso) : null;
  const snapshotMs = snapshot?.getTime();
  const nowMs = new Date(now).getTime();
  const clock = Number.isFinite(snapshotMs)
    ? `${String(snapshot.getUTCHours()).padStart(2, "0")}:${String(
        snapshot.getUTCMinutes(),
      ).padStart(2, "0")}`
    : null;
  const minutes =
    Number.isFinite(snapshotMs) && Number.isFinite(nowMs)
      ? Math.max(0, Math.floor((nowMs - snapshotMs) / 60_000))
      : null;

  let label = "unknown";
  if (minutes != null && minutes < 1) label = "just now";
  else if (minutes != null && minutes < 60) label = `${minutes} min ago`;
  else if (minutes != null) {
    const hours = Math.floor(minutes / 60);
    const remainder = String(minutes % 60).padStart(2, "0");
    label = `${hours} h ${remainder} min ago`;
  }

  return {
    iso,
    clock,
    minutes,
    label,
    stale: minutes == null || minutes > staleAfterMinutes,
  };
}
