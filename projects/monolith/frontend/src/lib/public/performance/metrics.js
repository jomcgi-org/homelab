/** Statistics shared by the CLI and its CI regression tests. */
export function median(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function percentile(values, fraction) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)];
}

// CLS is the largest session window: gaps under 1s, total duration under 5s.
// Layout shifts following recent input do not contribute.
export function cls(shifts) {
  let maximum = 0;
  let score = 0;
  let first = -Infinity;
  let previous = -Infinity;
  for (const shift of [...shifts].sort((a, b) => a.startTime - b.startTime)) {
    if (shift.hadRecentInput) continue;
    if (shift.startTime - previous < 1000 && shift.startTime - first < 5000) {
      score += shift.value;
    } else {
      first = shift.startTime;
      score = shift.value;
    }
    previous = shift.startTime;
    maximum = Math.max(maximum, score);
  }
  return maximum;
}

// CDP response bytes include the headers received so far. Data events add body
// chunks; loadingFinished replaces the running estimate with the final total.
export function recordTransferBytes(request, event, encodedDataLength) {
  if (!Number.isFinite(encodedDataLength)) return;
  request.transferBytes =
    event === "data"
      ? (request.transferBytes ?? 0) + encodedDataLength
      : encodedDataLength;
}

export function transferTotals(requests) {
  const known = requests.filter((r) => Number.isFinite(r.transferBytes));
  return {
    requestCount: requests.length,
    transferBytes: known.reduce((sum, r) => sum + r.transferBytes, 0),
    javascriptBytes: known
      .filter((r) => r.type === "Script")
      .reduce((sum, r) => sum + r.transferBytes, 0),
    incompleteRequests: requests.filter((r) => !r.complete).length,
    failedRequests: requests.filter((r) => r.failed).length,
  };
}

// Context-wide request events include dedicated workers and child frames.
// Request.sizes() supplies encoded response body plus response-header bytes.
// A missing final size must never silently contribute zero to a full total.
export async function responseTransferSize(request) {
  try {
    const response = await request.response();
    if (!response) throw new Error("response unavailable for sizing");
    if ([301, 302, 303, 307, 308].includes(response.status())) {
      // Chromium does not report redirect body transfer sizes. Playwright uses
      // Content-Length and silently substitutes zero when the header is absent.
      const length = await response.headerValue("content-length");
      if (length === null || !/^\d+$/.test(length.trim())) {
        throw new Error(
          "redirect body size unavailable without Content-Length",
        );
      }
    }
    const sizes = await request.sizes();
    if (
      ![sizes.responseBodySize, sizes.responseHeadersSize].every(
        (size) => Number.isFinite(size) && size >= 0,
      )
    ) {
      throw new Error("response size unavailable");
    }
    return {
      transferBytes: sizes.responseBodySize + sizes.responseHeadersSize,
    };
  } catch (error) {
    return { transferBytes: null, sizeError: error.message };
  }
}

export function responseTransferTotals(requests) {
  const measured = requests.filter(
    (r) => r.complete && !r.failed && Number.isFinite(r.transferBytes),
  );
  const scripts = requests.filter((r) => r.type === "Script");
  const scriptMeasured = measured.filter((r) => r.type === "Script");
  const total = measured.reduce((sum, r) => sum + r.transferBytes, 0);
  return {
    requestCount: requests.length,
    transferBytes: measured.length === requests.length ? total : null,
    javascriptBytes:
      scriptMeasured.length === scripts.length
        ? scriptMeasured.reduce((sum, r) => sum + r.transferBytes, 0)
        : null,
    completedResponseBytes: total,
    incompleteRequests: requests.filter((r) => !r.complete).length,
    failedRequests: requests.filter((r) => r.failed).length,
    unknownSizeRequests: requests.filter(
      (r) => !Number.isFinite(r.transferBytes),
    ).length,
  };
}

export function summarize(runs) {
  const successful = runs.filter(
    (r) => !r.error && !r.interactionError && r.status >= 200 && r.status < 400,
  );
  const metrics = [
    "ttfbMs",
    "lcpMs",
    "cls",
    "interactionLatencyMs",
    "transferBytes",
    "javascriptBytes",
    "requestCount",
  ];
  const unobservedTargetRuns = successful.filter(
    (r) => r.unobservedTargets,
  ).length;
  return {
    runs: runs.length,
    successfulRuns: successful.length,
    failedInteractions: runs.filter((r) => r.interactionError).length,
    completeProfileRuns: successful.filter((r) => r.profileComplete).length,
    unobservedTargetRuns,
    ...Object.fromEntries(
      metrics.map((key) => [
        key,
        ["transferBytes", "javascriptBytes", "requestCount"].includes(key) &&
        successful.some((r) => !Number.isFinite(r[key]))
          ? null
          : median(successful.map((r) => r[key])),
      ]),
    ),
    // A synthetic interaction sample is not field INP.
    inpMs: null,
    cacheStatus: runs.map(
      (r) => r.headers?.["cf-cache-status"] ?? "unavailable",
    ),
  };
}
