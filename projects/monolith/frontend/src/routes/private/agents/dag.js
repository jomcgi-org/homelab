export function computeRanks(nodes) {
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const memo = new Map();
  const rankOf = (node) => {
    if (memo.has(node.key)) return memo.get(node.key);
    memo.set(node.key, 0);
    const deps = (node.deps || []).map((key) => byKey.get(key)).filter(Boolean);
    const rank = deps.length ? Math.max(...deps.map(rankOf)) + 1 : 0;
    memo.set(node.key, rank);
    return rank;
  };
  const groups = [];
  nodes.forEach((node) => (groups[rankOf(node)] ||= []).push(node));
  return groups;
}

export function nodeIconKey(node) {
  if (node.state === "blocked")
    return node.blocked_on?.kind === "human" ? "blocked_human" : "blocked_dep";
  if (node.kind === "expansion") return "expansion";
  if (node.kind === "gate")
    return node.state === "passed" ? "gate_passed" : "gate";
  return (
    {
      done: "done",
      completed: "done",
      running: "running",
      working: "running",
      reviewing: "running",
      queued: "queued",
      future: "future",
      escalated: "escalated",
      needs_input: "escalated",
      stranded: "escalated",
      changes_requested: "escalated",
      failed: "failed",
      warn: "failed",
      cancelled: "cancelled",
      waiting: "gate",
      refused: "gate",
    }[node.state] || "future"
  );
}

export function nodeStateClass(node) {
  if (node.state === "blocked")
    return node.blocked_on?.kind === "human" ? "g-blocked-h" : "g-blocked-d";
  const aliasClass = {
    completed: "g-done",
    working: "g-running pulse",
    reviewing: "g-running pulse",
    needs_input: "g-blocked-h",
    stranded: "g-blocked-h",
    changes_requested: "g-blocked-h",
    warn: "g-failed",
  }[node.state];
  if (aliasClass) return aliasClass;
  return `g-${node.state}${node.state === "running" ? " pulse" : ""}`;
}

export function pipClass(attempt) {
  return attempt.state === "done"
    ? "pip"
    : attempt.state === "running"
      ? "pip run"
      : "pip bad";
}

export function capacityPips(plan, node) {
  const spent = (node.attempts || []).map(pipClass);
  if (!plan?.pinned || typeof plan.max_attempts !== "number") return spent;
  const free = Math.max(0, plan.max_attempts - spent.length);
  return [...spent, ...Array.from({ length: free }, () => "pip free")];
}
