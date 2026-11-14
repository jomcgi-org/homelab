// Match the server's per-response token, including when Kit reuses prefetched
// data. Starting the lease before that request consumes origin work, transit,
// hydration and preload residence without trusting the browser's wall clock.
export function overviewDeadline(overview, timing) {
  if (
    !overview?.leaseToken ||
    !(overview.leaseMs > 0) ||
    !Number.isFinite(overview.leaseMs)
  )
    return 0;
  const entries = [
    ...timing.getEntriesByType("navigation"),
    ...timing.getEntriesByType("resource"),
  ];
  const response = entries.find((entry) =>
    entry.serverTiming?.some(
      (metric) =>
        metric.name === "notes-lease" &&
        metric.description === overview.leaseToken,
    ),
  );
  // Older browsers or an evicted timing entry cannot safely grant a new lease.
  // Document start is conservative for both initial and client navigations.
  const start =
    response && Number.isFinite(response.startTime) && response.startTime >= 0
      ? response.startTime
      : 0;
  return start + overview.leaseMs;
}
