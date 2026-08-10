import { statusClass } from "./status.js";

const SUMMARY_ORDER = [
  "running",
  "working",
  "needs_input",
  "warn",
  "completed",
];

export function groupSessions(sessions) {
  const groups = new Map();

  for (const session of sessions) {
    const workflowId = session?.workflow_id;
    if (workflowId == null || workflowId === "") continue;
    const key = String(workflowId);
    const group = groups.get(key) ?? [];
    group.push(session);
    groups.set(key, group);
  }

  const entries = [];
  const emittedGroups = new Set();
  for (const session of sessions) {
    const workflowId = session?.workflow_id;
    if (workflowId == null || workflowId === "") {
      entries.push({ kind: "session", session });
      continue;
    }

    const key = String(workflowId);
    const members = groups.get(key);
    if (members.length === 1 || emittedGroups.has(key)) {
      if (members.length === 1) entries.push({ kind: "session", session });
      continue;
    }

    emittedGroups.add(key);
    const counts = {};
    for (const member of members) {
      const cls = statusClass(member);
      counts[cls] = (counts[cls] ?? 0) + 1;
    }
    entries.push({
      kind: "group",
      workflowId: key,
      sessions: members,
      counts,
    });
  }
  return entries;
}

export function groupSummary(counts) {
  return SUMMARY_ORDER.filter((status) => counts?.[status] > 0)
    .map((status) => `${counts[status]} ${status}`)
    .join(" · ");
}

export function isGroupExpanded(entry, { active, selectedId, expanded }) {
  const explicit = expanded?.[entry.workflowId];
  if (typeof explicit === "boolean") return explicit;

  return (
    active ||
    entry.sessions.some((session) => String(session.id) === String(selectedId))
  );
}
