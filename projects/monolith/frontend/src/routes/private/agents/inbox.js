import { isInFlight, runActivityAt } from "./run-history.js";

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
    activityAt:
      session.last_turn_at ?? session.updated_at ?? session.created_at,
    vm: vms?.[session.ember_session_id] ?? null,
  };
}

function newestFirst(a, b) {
  return parsedActivity(b.activityAt) - parsedActivity(a.activityAt);
}

function standalone(session) {
  return session?.workflow_id == null || session.workflow_id === "";
}

export function inboxGroups(runs = [], sessions = [], vms = {}) {
  const runItems = (runs ?? []).map(runItem);
  const sessionItems = (sessions ?? [])
    .filter(standalone)
    .map((session) => sessionItem(session, vms));

  return {
    needsYou: [
      ...runItems.filter((item) => Boolean(item.value.needs)),
      ...sessionItems.filter((item) => item.value.status === "needs_input"),
    ].sort(newestFirst),
    running: [
      ...runItems.filter((item) => isInFlight(item.value) && !item.value.needs),
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
