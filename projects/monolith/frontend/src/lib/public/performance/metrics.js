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
  let status;
  try {
    const response = await request.response();
    if (!response) throw new Error("response unavailable for sizing");
    status = response.status();
    if ([301, 302, 303, 307, 308].includes(status)) {
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
      status,
      responseBodySize: sizes.responseBodySize,
      responseHeadersSize: sizes.responseHeadersSize,
      contentLength: (await response.headerValue?.("content-length")) ?? null,
      transferBytes: sizes.responseBodySize + sizes.responseHeadersSize,
    };
  } catch (error) {
    return {
      ...(status === undefined ? {} : { status }),
      transferBytes: null,
      sizeError: error.message,
    };
  }
}

// Chromium worker bootstrap responses can report only header bytes. Do not
// mistake Playwright's missing-body fallback for a measured empty worker.
export function workerTransferSize(item, workerUrls) {
  const length = item.contentLength?.trim();
  const declaredBodySize =
    /^\d+$/.test(length ?? "") && Number.isFinite(Number(length))
      ? Number(length)
      : null;
  if (
    workerUrls.has(item.url) &&
    item.status === 200 &&
    ((item.responseBodySize === 0 && declaredBodySize !== 0) ||
      (declaredBodySize !== null && item.responseBodySize !== declaredBodySize))
  ) {
    return {
      ...item,
      reportedTransferBytes: item.transferBytes,
      knownResponseBytes: item.responseHeadersSize,
      transferBytes: null,
      sizeError:
        "worker body transfer size unavailable or inconsistent with Content-Length",
    };
  }
  return item;
}

// Pinned Chromium 151 returns unadjusted network timing through NavigationTiming.
// CDP responseReceived is emitted when headers are available after throttling.
// Retain both measurements; only the main navigation (including redirects) counts.
export function navigationHeaderMetrics(requests, navigation) {
  const main = requests.filter((r) => r.mainNavigation).at(-1);
  return {
    ttfbMs: Number.isFinite(main?.responseReceivedMs)
      ? main.responseReceivedMs
      : null,
    rawNavigationTtfbMs:
      Number.isFinite(navigation?.responseStart) &&
      Number.isFinite(navigation?.startTime)
        ? navigation.responseStart - navigation.startTime
        : null,
  };
}

export function responseTransferTotals(requests) {
  const measured = requests.filter(
    (r) => r.complete && !r.failed && Number.isFinite(r.transferBytes),
  );
  const scripts = requests.filter((r) => r.type === "Script");
  const scriptMeasured = measured.filter((r) => r.type === "Script");
  const total = measured.reduce((sum, r) => sum + r.transferBytes, 0);
  const knownPartial = requests.filter(
    (r) => r.complete && !r.failed && !Number.isFinite(r.transferBytes),
  );
  const lowerBound =
    total +
    knownPartial.reduce(
      (sum, r) =>
        sum +
        (Number.isFinite(r.knownResponseBytes) ? r.knownResponseBytes : 0),
      0,
    );
  return {
    requestCount: requests.length,
    transferBytes: measured.length === requests.length ? total : null,
    javascriptBytes:
      scriptMeasured.length === scripts.length
        ? scriptMeasured.reduce((sum, r) => sum + r.transferBytes, 0)
        : null,
    httpErrorRequests: requests.filter((r) => r.status >= 400).length,
    completedResponseBytes: lowerBound,
    completedJavascriptBytes:
      scriptMeasured.reduce((sum, r) => sum + r.transferBytes, 0) +
      knownPartial
        .filter((r) => r.type === "Script")
        .reduce(
          (sum, r) =>
            sum +
            (Number.isFinite(r.knownResponseBytes) ? r.knownResponseBytes : 0),
          0,
        ),
    incompleteRequests: requests.filter((r) => !r.complete).length,
    failedRequests: requests.filter((r) => r.failed).length,
    unknownSizeRequests: requests.filter(
      (r) => !Number.isFinite(r.transferBytes),
    ).length,
  };
}

export function summarize(runs) {
  const successful = runs.filter(
    (r) =>
      !r.error &&
      !r.interactionError &&
      r.comparisonEligible !== false &&
      r.status >= 200 &&
      r.status < 400,
  );
  const metrics = [
    "ttfbMs",
    "rawNavigationTtfbMs",
    "visibleContentMs",
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
    comparableInteractionRuns: successful.filter(
      (r) =>
        Number.isFinite(r.interactionLatencyMs) &&
        r.interactionComparable !== false,
    ).length,
    unreadyInteractionRuns: successful.filter(
      (r) => r.interactionComparable === false,
    ).length,
    nonComparableRuns: runs.filter((r) => r.comparisonEligible === false)
      .length,
    failedInteractions: runs.filter((r) => r.interactionError).length,
    completeProfileRuns: successful.filter((r) => r.profileComplete).length,
    unobservedTargetRuns,
    observedContentRuns: successful.filter((r) =>
      Number.isFinite(r.visibleContentMs),
    ).length,
    ...Object.fromEntries(
      metrics.map((key) => [
        key,
        [
          "transferBytes",
          "javascriptBytes",
          "requestCount",
          "visibleContentMs",
        ].includes(key) && successful.some((r) => !Number.isFinite(r[key]))
          ? null
          : median(
              successful
                .filter(
                  (r) =>
                    key !== "interactionLatencyMs" ||
                    r.interactionComparable !== false,
                )
                .map((r) => r[key]),
            ),
      ]),
    ),
    // A synthetic interaction sample is not field INP.
    inpMs: null,
    cacheStatus: runs.map(
      (r) => r.headers?.["cf-cache-status"] ?? "unavailable",
    ),
  };
}

// A local HTTP control checks that the browser's delivered-header event sees
// emulated latency. This validates only the fixture's page target, not workers.
export function summarizeTimingCalibration(samples) {
  const criteria = {
    pairs: 3,
    minimumThrottledHeaderMs: 140,
    minimumAddedLatencyMs: 100,
    maximumAddedLatencyMs: 400,
  };
  const controls = samples.filter((s) => s.latencyMs === 0);
  const throttled = samples.filter((s) => s.latencyMs === 150);
  const differences = throttled.flatMap((sample) => {
    const control = controls.find(
      (candidate) => candidate.pair === sample.pair,
    );
    return Number.isFinite(sample.headerArrivalMs) &&
      Number.isFinite(control?.headerArrivalMs)
      ? [sample.headerArrivalMs - control.headerArrivalMs]
      : [];
  });
  const minimumThrottledHeaderMs =
    throttled.length &&
    throttled.every((s) => Number.isFinite(s.headerArrivalMs))
      ? Math.min(...throttled.map((s) => s.headerArrivalMs))
      : null;
  const medianAddedLatencyMs = median(differences);
  return {
    criteria,
    minimumThrottledHeaderMs,
    medianControlHeaderMs: median(controls.map((s) => s.headerArrivalMs)),
    medianThrottledHeaderMs: median(throttled.map((s) => s.headerArrivalMs)),
    medianAddedLatencyMs,
    passed:
      samples.length === criteria.pairs * 2 &&
      controls.length === criteria.pairs &&
      throttled.length === criteria.pairs &&
      new Set(throttled.map((s) => s.pair)).size === criteria.pairs &&
      differences.length === criteria.pairs &&
      samples.every((s) => s.status === 200) &&
      minimumThrottledHeaderMs >= criteria.minimumThrottledHeaderMs &&
      medianAddedLatencyMs >= criteria.minimumAddedLatencyMs &&
      medianAddedLatencyMs <= criteria.maximumAddedLatencyMs,
  };
}

// A request is ready only after its entire successful response has finished.
// This says nothing about subsequent parsing, component state or map paint.
export function interactionResourceReady(requests, url) {
  return requests.some(
    (request) =>
      request.url === url &&
      request.complete &&
      !request.failed &&
      request.status >= 200 &&
      request.status < 300,
  );
}
