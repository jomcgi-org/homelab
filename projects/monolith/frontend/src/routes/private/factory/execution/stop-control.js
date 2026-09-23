export function makeStopRequest(identity) {
  if (!identity?.turn_seq || !identity?.dispatch_id) return null;
  return {
    turn_seq: identity.turn_seq,
    dispatch_id: identity.dispatch_id,
  };
}

export function stopInFlight(status) {
  return status?.outcome === "pending" || status?.outcome === "requested";
}

export function reconcileStop(status, detail) {
  if (!status) return null;
  const turn = (detail?.turns ?? []).find(
    (candidate) => Number(candidate.seq) === Number(status.turn_seq),
  );
  if (turn) {
    return {
      ...status,
      outcome:
        turn.terminal_reason === "user_interrupt" ? "confirmed" : "completed",
      terminal_reason: turn.terminal_reason ?? null,
    };
  }

  if (!stopInFlight(status)) return status;
  const active = detail?.stop_control?.active;
  if (!active || active.dispatch_id !== status.dispatch_id) {
    return {
      ...status,
      outcome: "unknown",
      reason: "terminal result unavailable",
    };
  }
  return status;
}
