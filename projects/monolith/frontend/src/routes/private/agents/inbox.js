import { isInFlight, runActivityAt } from "./run-history.js";
import { sessionActivityAt } from "./jump.js";
import { RUN_LEXICON as P } from "./run-lexicon.js";

function parsedActivity(value) {
  if (!value) return 0;
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  const parsed = Date.parse(normalized);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function runItem(run) {
  return {
    kind: "run",
    id: run.workflow_id,
    value: run,
    activityAt: runActivityAt(run),
  };
}

function sessionItem(session, vms) {
  return {
    kind: "session",
    id: session.id,
    value: session,
    activityAt: sessionActivityAt(session),
    vm: vms?.[session.ember_session_id] ?? null,
  };
}

function newestFirst(a, b) {
  return parsedActivity(b.activityAt) - parsedActivity(a.activityAt);
}

function standalone(session) {
  return session?.workflow_id == null || session.workflow_id === "";
}

export function humanDecision(run) {
  if (run?.needs?.kind === "human") return run.needs;
  const candidates = [
    run,
    run?.current,
    run?.current_node,
    run?.current?.node,
    ...(run?.shape ?? []),
    ...(run?.nodes ?? []),
  ];
  return (
    candidates.find((node) => node?.blocked_on?.kind === "human")?.blocked_on ??
    null
  );
}

export function runAsk(run) {
  const blockedOn = humanDecision(run);
  const decisionKind = run?.needs?.decision_kind ?? blockedOn?.decision_kind;
  if (decisionKind === "push_gate") return P.labels.approvePush;
  if (decisionKind === "review_escalation") return P.labels.decide;
  if (blockedOn) return P.labels.needsYou;
  const escalated =
    run?.state === "escalated" ||
    [...(run?.shape ?? []), ...(run?.nodes ?? [])].some(
      (node) => node?.state === "escalated",
    );
  return escalated ? P.labels.escalated : P.labels.needsYou;
}

const RECENT_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

export function inboxGroups(runs = [], sessions = [], vms = {}) {
  const runItems = (runs ?? []).map(runItem);
  const sessionItems = (sessions ?? [])
    .filter(standalone)
    .map((session) => sessionItem(session, vms));

  return {
    needsYou: [
      ...runItems.filter(
        (item) =>
          Boolean(item.value.needs) || Boolean(humanDecision(item.value)),
      ),
      ...sessionItems.filter((item) =>
        ["needs_input", "awaiting_login"].includes(item.value.status),
      ),
    ].sort(newestFirst),
    running: [
      ...runItems.filter(
        (item) =>
          isInFlight(item.value) &&
          !item.value.needs &&
          !humanDecision(item.value),
      ),
      ...sessionItems.filter((item) => item.value.status === "running"),
    ].sort(newestFirst),
  };
}

export function arrivalSelection(needsYou = [], running = []) {
  const item = (needsYou ?? [])[0] ?? (running ?? [])[0];
  if (
    !item ||
    item.id == null ||
    (item.kind !== "run" && item.kind !== "session")
  )
    return null;
  return { kind: item.kind, id: item.id };
}

export function railState({ needsYou = 0, running = 0, manual = null } = {}) {
  if (manual === "folded" || manual === "open") return manual;
  return Number(needsYou) === 0 && Number(running) === 0 ? "folded" : "open";
}

export function jumpTotal(sessions = [], runs = [], terminalRuns = []) {
  return (
    (sessions ?? []).filter(standalone).length +
    (runs ?? []).length +
    (terminalRuns ?? []).length
  );
}

export function recentSummary(sessions = [], runs = [], now = Date.now()) {
  const nowMs = now instanceof Date ? now.getTime() : Number(now);
  const allItems = [
    ...(sessions ?? [])
      .filter(standalone)
      .map((session) => sessionItem(session)),
    ...(runs ?? []).map(runItem),
  ];
  const items = allItems
    .map((item) => ({ ...item, at: parsedActivity(item.activityAt) }))
    .filter(
      (item) =>
        item.at > 0 && item.at <= nowMs && nowMs - item.at < RECENT_WINDOW_MS,
    )
    .sort((a, b) => b.at - a.at);

  return {
    items: items.slice(0, 5),
    count: items.length,
    allCount: allItems.length,
    sessionCount: items.filter((item) => item.kind === "session").length,
    runCount: items.filter((item) => item.kind === "run").length,
    spend: items.reduce(
      (sum, item) =>
        sum +
        Number(
          item.kind === "run"
            ? item.value?.cost_usd || 0
            : item.value?.total_cost_usd || 0,
        ),
      0,
    ),
  };
}
