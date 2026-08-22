export function computeRanks(nodes) {
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const memo = new Map();
  const visiting = new Set();
  const rankOf = (node) => {
    if (memo.has(node.key)) return memo.get(node.key);
    if (visiting.has(node.key)) return 0;
    visiting.add(node.key);
    const deps = (node.deps || []).map((key) => byKey.get(key)).filter(Boolean);
    const rank = deps.length ? Math.max(...deps.map(rankOf)) + 1 : 0;
    visiting.delete(node.key);
    memo.set(node.key, rank);
    return rank;
  };
  const groups = [];
  nodes.forEach((node) => (groups[rankOf(node)] ||= []).push(node));
  return groups.filter(Array.isArray);
}

export function layoutEdges(ranks) {
  const safeRanks = (ranks ?? []).filter(Array.isArray);
  return safeRanks.slice(0, -1).map((rank) => {
    const strong =
      rank.length > 0 &&
      rank.every((node) => ["done", "passed"].includes(node.state));
    return { dim: !strong, strong };
  });
}

export function defaultSelectedKey(nodes) {
  const attention = nodes.find(
    (node) => node.state === "escalated" || node.blocked_on?.kind === "human",
  );
  if (attention) return attention.key;
  const running = nodes.find((node) => node.state === "running");
  if (running) return running.key;
  const done = nodes.filter((node) => node.state === "done").at(-1);
  return done?.key ?? nodes[0]?.key ?? null;
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

export function shapeStateClass(run, node) {
  if (
    run?.needs?.kind === "human" &&
    node.state === "blocked" &&
    run.current?.state === "blocked"
  ) {
    return "g-blocked-h";
  }
  return nodeStateClass(node);
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
