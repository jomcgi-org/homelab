import { firstLine } from "./run-format.js";
import { relativeTime, runActivityAt } from "./run-history.js";
import { RUN_LEXICON as P } from "./run-lexicon.js";

export function sessionActivityAt(session) {
  return session?.last_turn_at ?? session?.created_at;
}

function parsedActivity(value) {
  if (!value) return 0;
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  const parsed = Date.parse(normalized);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function sessionTitle(session) {
  return (
    firstLine(session?.title) ||
    (session?.repo
      ? `${session.repo}@${session.branch || P.labels.defaultBranch}`
      : "") ||
    String(session?.local_session_id || session?.id || P.labels.session).slice(
      0,
      8,
    )
  );
}

function runTitle(run) {
  return firstLine(run?.title || run?.task?.text) || String(run?.workflow_id);
}

function segmentsFor(title, query) {
  const needle = query.trim();
  if (!needle) return [{ text: title, hit: false }];
  const start = title.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase());
  if (start < 0) return [{ text: title, hit: false }];
  const end = start + needle.length;
  return [
    ...(start ? [{ text: title.slice(0, start), hit: false }] : []),
    { text: title.slice(start, end), hit: true },
    ...(end < title.length ? [{ text: title.slice(end), hit: false }] : []),
  ];
}

function stateWord(value, fallback) {
  const key = String(value || "").toLocaleLowerCase();
  return P.stateWords[key] || key || fallback;
}

function meta(state, activityAt) {
  return `${state} ${P.punct.dot} ${relativeTime(activityAt)}`;
}

function inboxResult(item, group, query, runsById) {
  const value =
    item.kind === "run"
      ? (item.value ?? runsById.get(String(item.id)))
      : item.value;
  const title = item.kind === "run" ? runTitle(value) : sessionTitle(value);
  return {
    kind: item.kind,
    id: item.id,
    title,
    meta: meta(group, item.activityAt),
    segments: segmentsFor(title, query),
  };
}

function earlierSession(session, query) {
  const title = sessionTitle(session);
  const activityAt = sessionActivityAt(session);
  return {
    kind: "session",
    id: session.id,
    title,
    meta: meta(stateWord(session.status, P.stateWords.completed), activityAt),
    segments: segmentsFor(title, query),
    activityAt,
  };
}

function earlierRun(run, query) {
  const title = runTitle(run);
  const activityAt = runActivityAt(run);
  return {
    kind: "run",
    id: run.workflow_id,
    title,
    meta: meta(
      stateWord(run.state || run.dbos_status, P.stateWords.completed),
      activityAt,
    ),
    segments: segmentsFor(title, query),
    activityAt,
  };
}

function keyFor(item) {
  return `${item.kind}:${String(item.id)}`;
}

export function jumpMatches(
  query,
  { sessions = [], runs = [], terminalRuns = [], inbox = {} } = {},
) {
  const needle = String(query ?? "").trim();
  const seen = new Set();
  const inboxItems = [];
  const runsById = new Map(
    (runs ?? []).map((run) => [String(run.workflow_id), run]),
  );

  for (const [items, label] of [
    [inbox.needsYou ?? [], P.labels.needsYou],
    [inbox.running ?? [], P.labels.runningGroup],
  ]) {
    for (const item of items) {
      const key = keyFor(item);
      if (seen.has(key)) continue;
      seen.add(key);
      inboxItems.push(inboxResult(item, label, needle, runsById));
    }
  }

  const earlierByKey = new Map();
  // A session a run spawned is a detail of that run, never a peer: same
  // rule as inbox.js and the page's isStandalone.
  const standalone = (s) => s?.workflow_id == null || s.workflow_id === "";
  for (const session of (sessions ?? []).filter(standalone)) {
    const item = earlierSession(session, needle);
    const key = keyFor(item);
    if (!seen.has(key) && !earlierByKey.has(key)) earlierByKey.set(key, item);
  }
  for (const run of terminalRuns ?? []) {
    const item = earlierRun(run, needle);
    const key = keyFor(item);
    if (!seen.has(key) && !earlierByKey.has(key)) earlierByKey.set(key, item);
  }

  let earlierItems = [...earlierByKey.values()].sort(
    (a, b) => parsedActivity(b.activityAt) - parsedActivity(a.activityAt),
  );
  if (needle) {
    const matches = (item) =>
      item.title.toLocaleLowerCase().includes(needle.toLocaleLowerCase());
    return {
      inbox: inboxItems.filter(matches),
      earlier: earlierItems
        .filter(matches)
        .map(({ activityAt, ...item }) => item),
    };
  }

  earlierItems = earlierItems.slice(0, 8);
  return {
    inbox: inboxItems,
    earlier: earlierItems.map(({ activityAt, ...item }) => item),
  };
}

export function jumpActions(query) {
  const text = String(query ?? "").trim();
  if (!text) {
    return [
      {
        kind: "voice",
        id: "action-voice",
        title: P.labels.openVoiceCompanion,
        hint: "",
      },
      {
        kind: "new",
        id: "action-new",
        title: P.labels.jumpNewSession,
        hint: "",
      },
    ];
  }
  return [
    {
      kind: "voice",
      id: "action-voice",
      title: P.labels.openVoiceCompanion,
      hint: "",
    },
    {
      kind: "new",
      id: "action-new",
      title: `${P.labels.jumpNewSessionWith} ${P.labels.quoteMark}${text}${P.labels.quoteMark}`,
      hint: P.labels.shortcutEnter,
    },
    {
      kind: "search",
      id: "action-search",
      title: `${P.labels.jumpSearchTurnsFor} ${P.labels.quoteMark}${text}${P.labels.quoteMark}`,
      hint: P.labels.shortcutShiftEnter,
    },
  ];
}
