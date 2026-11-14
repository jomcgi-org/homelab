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

// The document's clock pair exists before any response, hydration or preload.
// Anchoring at load() would miss device sleep before that call. timeOrigin and
// Date.now use the same client epoch; only their elapsed difference is used.
export function overviewClock(overview, timing) {
  if (!overview || !Number.isFinite(timing.timeOrigin)) return null;
  return { monotonicAt: 0, wallAt: timing.timeOrigin };
}

export function overviewLeaseCurrent(
  deadline,
  clock,
  monotonicNow = performance.now(),
  wallNow = Date.now(),
) {
  if (!clock) return false;
  const remaining = deadline - clock.monotonicAt;
  const monotonicElapsed = monotonicNow - clock.monotonicAt;
  const wallElapsed = wallNow - clock.wallAt;
  // Some platforms pause performance.now and timers during device sleep.
  // Either elapsed clock can end the lease; neither can lengthen it. A clock
  // discontinuity fails closed, allowing 100ms for timer precision rounding.
  return (
    Number.isFinite(remaining) &&
    monotonicElapsed >= 0 &&
    wallElapsed >= 0 &&
    Math.abs(wallElapsed - monotonicElapsed) <= 100 &&
    Math.max(monotonicElapsed, wallElapsed) < remaining
  );
}
