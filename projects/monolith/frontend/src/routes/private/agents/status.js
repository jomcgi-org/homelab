export function statusClass(session) {
  if (session?.status === "running") {
    return Number(session?.pending_count) > 0 ? "working" : "running";
  }
  if (session?.status === "warn") return "warn";
  if (session?.status === "needs_input") return "needs_input";
  return "completed";
}

export function statusLabel(session) {
  if (session?.status === "running") {
    return Number(session?.pending_count) > 0 ? "working" : "running";
  }
  if (session?.status === "needs_input") return "needs input";
  if (session?.status === "completed") return "completed";
  if (session?.status === "warn") return "warn";
  return "completed";
}
