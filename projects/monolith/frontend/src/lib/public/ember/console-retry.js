/**
 * Maps the backend classification string to a tier, or null when the result
 * is unclear. Transitional and unknown values are real measurements, but not
 * confident classifications, so they must never be written into best-times.
 * For example, a measured 12.2ms transitional run with phase_before=banking
 * was landing in "cold start" and destroying the comparison. These runs still
 * belong in history and the sparkline.
 */
export function classifyTier(classification) {
  if (
    classification === "warm" ||
    classification === "relight" ||
    classification === "cold"
  ) {
    return classification;
  }
  return null;
}

/**
 * Checks whether a measurement included VM snapshot write time. These runs
 * are excluded from best-times by classifyTier, but when shown in the last-run
 * row they need attribution so the visitor understands the time.
 */
export function includedSnapshotWait(body) {
  return (
    body?.phase_before === "banking" || body?.phase_before === "checkpointed"
  );
}

/**
 * Gates whether the next attempt starts, not the current attempt. Transient
 * failures are sub-second, such as in-band busy responses and mid-transition
 * refusals, so retries fit within the 6.5s delay window. A slow failure must
 * not be retried, keeping the worst case near 90s instead of 375+s. Legitimate
 * cold boots can take up to 60s (wakeTimeoutSeconds), so an in-flight attempt
 * must not be cut off.
 */
export function shouldRetry(elapsedMs, retryWindow) {
  return elapsedMs < retryWindow;
}

/**
 * Returns an indicative live VM phase. Status is cached server-side for about
 * 500ms and polled every 700ms, so this can lag reality by about 1s. It shows
 * what the VM is doing, not a synchronised readout, and helps prevent the
 * visitor from mistaking a wake for a hang. Unknown states stay silent rather
 * than inventing precision we do not have.
 */
export function phaseLabel(state) {
  switch (state) {
    case "banking":
    case "checkpointed":
      return "writing snapshot";
    case "relighting":
    case "starting":
      return "restoring";
    case "cold_booting":
      return "cold booting";
    case "banked":
      return "waking";
    default:
      return "";
  }
}
