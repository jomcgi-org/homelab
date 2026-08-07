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

// Coarse EmberVM guest state for a session, joined against the /agents/vms
// control-plane listing. "off" covers no binding, a binding the control
// plane no longer knows, and terminal states alike: in every case the next
// prompt boots fresh.
export function vmState(session, vms) {
  const vm = vms?.[session?.ember_session_id];
  if (vm?.state === "awake" || vm?.state === "asleep") return vm.state;
  return "off";
}
