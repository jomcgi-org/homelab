export const IN_FLIGHT_STATUSES = new Set(["PENDING", "ENQUEUED"]);

export function isInFlight(run) {
  return IN_FLIGHT_STATUSES.has(run?.dbos_status);
}

export function partitionRuns(runs) {
  return (runs ?? []).reduce(
    (result, run) => {
      result[isInFlight(run) ? "inFlight" : "terminal"].push(run);
      return result;
    },
    { inFlight: [], terminal: [] },
  );
}

export function runActivityAt(run) {
  return run?.completed_at ?? run?.updated_at ?? run?.created_at;
}

export function recentRuns(
  runs,
  now = Date.now(),
  windowMs = 24 * 60 * 60 * 1000,
) {
  return (runs ?? []).filter((run) => {
    const at = Date.parse(runActivityAt(run) ?? "");
    return !Number.isNaN(at) && now - at < windowMs;
  });
}
