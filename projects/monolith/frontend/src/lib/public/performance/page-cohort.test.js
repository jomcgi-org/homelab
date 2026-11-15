import { describe, expect, it } from "vitest";
import {
  pageCohort,
  pageRequest,
  pageDestination,
  sameDocumentUrl,
} from "./page-cohort.js";
import { createNavigationTracker } from "./navigation.js";

const origin = "https://jomcgi.dev";
const request = (requestId, url, extra = {}) => ({
  requestId,
  frameId: "main",
  type: "Document",
  timestamp: 100,
  request: { url },
  ...extra,
});

describe("page measurement cohorts", () => {
  it("keeps canonical URLs, logical query state and fragments unchanged by default", () => {
    const route = "/app/stars?mode=historical#map";
    expect(pageRequest(origin, route)).toEqual({
      route,
      canonicalUrl: origin + route,
      requestedUrl: origin + route,
      pageCache: { mode: "canonical", parameter: null, key: null },
    });
  });

  it("uses one repeatable key without changing logical selector or readiness lookup", () => {
    const route = "/app/stars";
    const cohort = pageCohort("run-123-attempt-2");
    const selectors = new Map([[route, "button.historical"]]);
    const readiness = new Map([[route, "/app/stars/history-map"]]);
    const visits = Array.from({ length: 3 }, () =>
      pageRequest(origin, route, cohort),
    );
    expect(new Set(visits.map((v) => v.requestedUrl)).size).toBe(1);
    expect(visits[0].requestedUrl).toBe(
      origin + route + "?__public_perf_page=run-123-attempt-2",
    );
    for (const visit of visits) {
      expect(selectors.get(visit.route)).toBe("button.historical");
      expect(new URL(readiness.get(visit.route), origin).href).toBe(
        origin + "/app/stars/history-map",
      );
    }
    expect(
      pageRequest(origin, route, pageCohort("run-124")).requestedUrl,
    ).not.toBe(visits[0].requestedUrl);
  });

  it("preserves existing page state and rejects unlabeled keys or unsafe tokens", () => {
    const keyed = pageRequest(
      origin,
      "/app/ships?mode=heat#map",
      pageCohort("release-B"),
    );
    const url = new URL(keyed.requestedUrl);
    expect([...url.searchParams]).toEqual([
      ["mode", "heat"],
      ["__public_perf_page", "release-B"],
    ]);
    expect(url.hash).toBe("#map");
    for (const key of ["", "x&mode=heat", "x y", "a".repeat(97), "☀"])
      expect(() => pageCohort(key)).toThrow();
    expect(() =>
      pageRequest(origin, "/app/stars?__public_perf_page=hidden"),
    ).toThrow();
    expect(() =>
      pageRequest(origin, "https://other.test/app/stars", pageCohort("x")),
    ).toThrow();
  });

  it("matches HTTP document identity including the key and recovery query", () => {
    const url = pageRequest(origin, "/app/stars", pageCohort("B")).requestedUrl;
    expect(sameDocumentUrl(url + "#map", url)).toBe(true);
    expect(sameDocumentUrl(origin + "/app/stars", url)).toBe(false);
    expect(sameDocumentUrl(url + "&_fresh=recovery", url)).toBe(false);
    expect(sameDocumentUrl(url.replace("=B", "=A"), url)).toBe(false);
    expect(sameDocumentUrl(undefined, url)).toBe(false);
  });

  it("accepts the intended redirect chain but marks its unkeyed destination", () => {
    const cohort = pageCohort("B");
    const visit = pageRequest(origin, "/app/firecracker", cohort);
    const tracker = createNavigationTracker("main", visit.requestedUrl);
    tracker.request(request("redirect", visit.requestedUrl));
    tracker.request(
      request("redirect", origin + "/ember/firecracker", {
        redirectResponse: { status: 301 },
      }),
    );
    expect(tracker.snapshot()).toMatchObject({
      comparisonEligible: true,
      requestedUrlMatched: true,
      navigationCount: 1,
    });
    expect(
      pageDestination(origin + "/ember/firecracker", cohort),
    ).toMatchObject({
      finalPageCacheKeyRetained: false,
      finalPageCacheMode: "key-not-retained",
    });
    expect(
      pageDestination(visit.requestedUrl, cohort).finalPageCacheKeyRetained,
    ).toBe(true);
  });

  it("rejects a mismatched initial key and a recovery reload even if the key survives", () => {
    const visit = pageRequest(origin, "/app/stars", pageCohort("B"));
    const wrong = createNavigationTracker("main", visit.requestedUrl);
    wrong.request(request("wrong", visit.canonicalUrl));
    expect(wrong.snapshot()).toMatchObject({
      comparisonEligible: false,
      requestedUrlMatched: false,
    });
    for (const recovered of [
      visit.requestedUrl + "&_fresh=x",
      visit.canonicalUrl + "?_fresh=x",
    ]) {
      const tracker = createNavigationTracker("main", visit.requestedUrl);
      tracker.request(request("first", visit.requestedUrl));
      tracker.request(request("reload", recovered));
      expect(tracker.snapshot()).toMatchObject({
        comparisonEligible: false,
        navigationCount: 2,
      });
    }
  });
});
