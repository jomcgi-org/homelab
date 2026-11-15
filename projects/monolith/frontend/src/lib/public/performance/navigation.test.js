import { describe, expect, it } from "vitest";
import { createNavigationTracker } from "./navigation.js";
import { summarize, interactionResourceReady } from "./metrics.js";
import {
  endpointCacheGroups,
  probeUrl,
  validateProbeEndpoints,
  PROBE_ENDPOINTS,
} from "./endpoint-probes.js";

const request = (requestId, timestamp, url, extra = {}) => ({
  requestId,
  timestamp,
  frameId: "main",
  type: "Document",
  request: { url },
  ...extra,
});

describe("document observation clocks", () => {
  it("excludes the deployed chunk-404 reload sequence from medians", () => {
    const tracker = createNavigationTracker("main");
    tracker.request(
      request("initial", 100, "/app/trips/2025-liard-hot-springs"),
    );
    tracker.request(request("child", 100.2, "/frame", { frameId: "child" }));
    tracker.request(
      request("chunk", 101, "/_app/immutable/nodes/28.oV_cqV0-.js", {
        type: "Script",
      }),
    );
    tracker.response(
      { requestId: "initial", timestamp: 100.2, response: { status: 200 } },
      { "cf-cache-status": "HIT" },
    );
    tracker.request(
      request(
        "reload",
        104.340683,
        "/app/trips/2025-liard-hot-springs?_fresh=mwh3r3kn",
      ),
    );
    tracker.response(
      { requestId: "reload", timestamp: 107.804208, response: { status: 200 } },
      { "cf-cache-status": "MISS" },
    );
    const state = tracker.snapshot();
    expect(state.navigationCount).toBe(2);
    expect(state.navigations[1].startMs).toBeCloseTo(4340.683);
    expect(state.navigations[1].responseReceivedMs).toBeCloseTo(7804.208);
    expect(state.comparisonEligible).toBe(false);
    expect(state.additionalDocumentNavigation).toBe(true);
    expect(state.navigations[1].headers["cf-cache-status"]).toBe("MISS");
    const summary = summarize([
      { status: 200, lcpMs: 6200, comparisonEligible: true },
      { status: 200, lcpMs: 6220, comparisonEligible: true },
      { status: 200, lcpMs: 5940, ...state },
    ]);
    expect(summary.successfulRuns).toBe(2);
    expect(summary.nonComparableRuns).toBe(1);
    expect(summary.lcpMs).toBe(6210);
  });

  it("keeps a same-ID redirect chain as one navigation and ignores child frames", () => {
    const tracker = createNavigationTracker("main");
    const first = request("redirect", 20, "/app/firecracker");
    tracker.request(first);
    tracker.request(
      request("redirect", 20.3, "/ember/firecracker", {
        redirectResponse: { status: 301 },
      }),
    );
    tracker.request(request("frame", 20.4, "/frame", { frameId: "frame" }));
    expect(tracker.isInitialDocument(first)).toBe(true);
    expect(tracker.isInitialDocument(request("other", 21, "/other"))).toBe(
      false,
    );
    const state = tracker.snapshot();
    expect(state.comparisonEligible).toBe(true);
    expect(state.navigationCount).toBe(1);
    expect(state.navigations[0].urls).toEqual([
      "/app/firecracker",
      "/ember/firecracker",
    ]);
    tracker.request(request("reload", 22, "/ember/firecracker"));
    expect(state.navigationCount).toBe(1);
    expect(tracker.snapshot().comparisonEligible).toBe(false);
  });

  it("cannot certify an observation without a top-level request", () => {
    expect(createNavigationTracker("main").snapshot().comparisonEligible).toBe(
      false,
    );
  });
});

describe("bounded anonymous edge probes", () => {
  it("accepts only the audited thirteen unique paths and at most twenty reads each", () => {
    expect(() => validateProbeEndpoints(PROBE_ENDPOINTS, 20)).not.toThrow();
    for (const paths of [
      [],
      ["/api/stars/sites"],
      ["/app/stars/map?mode=private"],
      ["https://other.example/app/stars/map"],
      [PROBE_ENDPOINTS[0], PROBE_ENDPOINTS[0]],
    ])
      expect(() => validateProbeEndpoints(paths, 20)).toThrow();
    for (const count of [0, 21, 1.5])
      expect(() => validateProbeEndpoints(PROBE_ENDPOINTS, count)).toThrow();
  });

  it("keeps the endpoint unchanged and creates independent query keys", () => {
    const first = new URL(
      probeUrl("https://jomcgi.dev", "/app/stars/map", "run-one", 1),
    );
    expect(first.origin).toBe("https://jomcgi.dev");
    expect(first.pathname).toBe("/app/stars/map");
    expect([...first.searchParams]).toEqual([
      ["__public_perf_probe", "run-one-1"],
    ]);
    expect(probeUrl(first.origin, first.pathname, "run-one", 2)).not.toBe(
      first.href,
    );
    expect(probeUrl(first.origin, first.pathname, "run-two", 1)).not.toBe(
      first.href,
    );
  });

  it("groups actual edge status without relabeling a probe as a miss", () => {
    const groups = endpointCacheGroups(
      [
        { headers: { "cf-cache-status": "HIT" }, totalMs: 10 },
        { headers: { "cf-cache-status": "MISS" }, totalMs: 500 },
        { error: "timeout" },
      ],
      (rows) => ({ totalMs: rows[0].totalMs ?? null }),
    );
    expect(groups).toEqual({
      HIT: { samples: 1, totalMs: 10 },
      MISS: { samples: 1, totalMs: 500 },
      unavailable: { samples: 1, totalMs: null },
    });
  });
});

describe("interaction resource evidence", () => {
  it("requires complete successful body transfer rather than headers or an unrelated request", () => {
    const url = "https://jomcgi.dev/app/stars/history-map";
    const response = { url, status: 200, complete: true };
    expect(interactionResourceReady([response], url)).toBe(true);
    for (const request of [
      { ...response, complete: false },
      { ...response, failed: "aborted" },
      { ...response, status: 500 },
      { ...response, url: "/other" },
    ])
      expect(interactionResourceReady([request], url)).toBe(false);
  });

  it("retains a cold click but excludes its loading-branch duration from comparable interaction medians", () => {
    const runs = [
      { status: 200, interactionLatencyMs: 40, interactionComparable: false },
      { status: 200, interactionLatencyMs: 1336, interactionComparable: true },
    ];
    const summary = summarize(runs);
    expect(summary.successfulRuns).toBe(2);
    expect(summary.interactionLatencyMs).toBe(1336);
    expect(summary.unreadyInteractionRuns).toBe(1);
    expect(summarize(runs.slice(0, 1)).interactionLatencyMs).toBe(null);
  });
});
