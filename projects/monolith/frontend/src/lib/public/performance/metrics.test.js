import { describe, expect, it, vi } from "vitest";
import {
  cls,
  median,
  recordTransferBytes,
  responseTransferSize,
  responseTransferTotals,
  percentile,
  summarize,
  transferTotals,
} from "./metrics.js";

describe("public app measurement statistics", () => {
  it.each([301, 302, 303, 307, 308])(
    "keeps a %i redirect without Content-Length and its page total unknown",
    async (status) => {
      const request = {
        response: async () => ({
          status: () => status,
          headerValue: async () => null,
        }),
        sizes: vi.fn(async () => ({
          responseBodySize: 0,
          responseHeadersSize: 150,
        })),
      };
      const redirect = await responseTransferSize(request);
      expect(redirect.transferBytes).toBeNull();
      expect(redirect.sizeError).toContain("without Content-Length");
      expect(request.sizes).not.toHaveBeenCalled();
      const total = responseTransferTotals([
        { type: "Document", complete: true, ...redirect },
        { type: "Document", complete: true, transferBytes: 1000 },
      ]);
      expect(total.transferBytes).toBeNull();
      expect(total.completedResponseBytes).toBe(1000);
      expect(total.unknownSizeRequests).toBe(1);
      expect(summarize([{ status: 200, ...total }]).transferBytes).toBeNull();
    },
  );

  it("accepts a redirect's explicit zero body length and ordinary body sizes", async () => {
    const redirect = await responseTransferSize({
      response: async () => ({
        status: () => 302,
        headerValue: async () => "0",
      }),
      sizes: async () => ({ responseBodySize: 0, responseHeadersSize: 150 }),
    });
    const document = await responseTransferSize({
      response: async () => ({ status: () => 200 }),
      sizes: async () => ({ responseBodySize: 800, responseHeadersSize: 200 }),
    });
    expect(
      responseTransferTotals([
        { type: "Document", complete: true, ...redirect },
        { type: "Document", complete: true, ...document },
      ]).transferBytes,
    ).toBe(1150);
  });

  it("preserves a failed sizing operation as unknown", async () => {
    const result = await responseTransferSize({
      response: async () => ({ status: () => 200 }),
      sizes: async () => {
        throw new Error("connection closed");
      },
    });
    expect(result).toEqual({
      transferBytes: null,
      sizeError: "connection closed",
    });
  });

  it("does not turn unobservable metrics or failed runs into zero", () => {
    expect(median([null, undefined, NaN])).toBeNull();
    const result = summarize([
      {
        status: 200,
        ttfbMs: 120,
        lcpMs: null,
        headers: { "cf-cache-status": "HIT" },
      },
      { status: 503, ttfbMs: 1, lcpMs: 5 },
      { error: "navigation timed out", ttfbMs: 1 },
    ]);
    expect(result.successfulRuns).toBe(1);
    expect(result.ttfbMs).toBe(120);
    expect(result.lcpMs).toBeNull();
    expect(result.inpMs).toBeNull();
    expect(result.interactionLatencyMs).toBeNull();
  });

  it("fails explicitly requested interactions but preserves unobservable successful samples", () => {
    const failed = summarize([
      { status: 200, interactionError: "selector timed out", ttfbMs: 100 },
    ]);
    expect(failed.successfulRuns).toBe(0);
    expect(failed.failedInteractions).toBe(1);
    expect(failed.ttfbMs).toBeNull();
    const fast = summarize([
      { status: 200, interactionLatencyMs: null, profileComplete: true },
    ]);
    expect(fast.successfulRuns).toBe(1);
    expect(fast.failedInteractions).toBe(0);
    expect(fast.interactionLatencyMs).toBeNull();
  });

  it("includes worker requests while refusing incomplete whole-page totals", () => {
    const complete = responseTransferTotals([
      { type: "Document", transferBytes: 1000, complete: true },
      { type: "Script", transferBytes: 500, complete: true },
      { type: "Fetch", transferBytes: 2000, complete: true, worker: true },
    ]);
    expect(complete.requestCount).toBe(3);
    expect(complete.transferBytes).toBe(3500);
    expect(complete.javascriptBytes).toBe(500);
    const partial = responseTransferTotals([
      { type: "Script", transferBytes: 500, complete: true },
      { type: "Fetch", transferBytes: null, complete: false, worker: true },
      {
        type: "Image",
        transferBytes: null,
        complete: true,
        failed: "disconnected",
      },
    ]);
    expect(partial.requestCount).toBe(3);
    expect(partial.transferBytes).toBeNull();
    expect(partial.completedResponseBytes).toBe(500);
    expect(partial.incompleteRequests).toBe(1);
    expect(partial.failedRequests).toBe(1);
    const summary = summarize([
      {
        status: 200,
        ...complete,
        profileComplete: false,
        unobservedTargets: true,
      },
      {
        status: 200,
        ...partial,
        profileComplete: false,
        unobservedTargets: true,
      },
    ]);
    expect(summary.transferBytes).toBeNull();
    expect(summary.requestCount).toBe(3);
    expect(summary.completeProfileRuns).toBe(0);
    expect(summary.unobservedTargetRuns).toBe(2);
  });

  it("retains received headers for unfinished transfers and replaces estimates on completion", () => {
    const request = { type: "Script", transferBytes: null, complete: false };
    recordTransferBytes(request, "response", 150);
    recordTransferBytes(request, "data", 200);
    recordTransferBytes(request, "data", 250);
    expect(transferTotals([request]).transferBytes).toBe(600);
    recordTransferBytes(request, "finished", 620);
    request.complete = true;
    expect(transferTotals([request]).transferBytes).toBe(620);
    request.failed = "disconnected";
    expect(transferTotals([request]).transferBytes).toBe(620);
  });

  it("uses a median and nearest-rank p95 for twenty endpoint samples", () => {
    const samples = Array.from({ length: 20 }, (_, i) => i + 1);
    expect(median(samples)).toBe(10.5);
    expect(percentile(samples, 0.95)).toBe(19);
    expect(percentile([], 0.95)).toBeNull();
  });

  it("uses the largest CLS window instead of lifetime accumulated shifts", () => {
    expect(
      cls([
        { startTime: 100, value: 0.02 },
        { startTime: 800, value: 0.03 },
        { startTime: 900, value: 0.9, hadRecentInput: true },
        { startTime: 1800, value: 0.04 },
        { startTime: 2400, value: 0.02 },
      ]),
    ).toBeCloseTo(0.06);
    expect(
      cls(
        Array.from({ length: 8 }, (_, i) => ({
          startTime: i * 900,
          value: 0.01,
        })),
      ),
    ).toBeCloseTo(0.06);
  });

  it("counts redirect hops and unfinished transfers without double-counting completion", () => {
    expect(
      transferTotals([
        { type: "Document", transferBytes: 200, complete: true },
        { type: "Document", transferBytes: 1000, complete: true },
        { type: "Script", transferBytes: 400, complete: true },
        { type: "Image", transferBytes: 100, complete: false },
        { type: "Script", transferBytes: null, complete: true, failed: true },
      ]),
    ).toEqual({
      requestCount: 5,
      transferBytes: 1700,
      javascriptBytes: 400,
      incompleteRequests: 1,
      failedRequests: 1,
    });
  });
});
