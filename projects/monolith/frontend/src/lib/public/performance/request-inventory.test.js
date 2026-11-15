import { EventEmitter } from "node:events";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ensureRequestInventory,
  observeContextRequests,
  withinDeadline,
} from "./request-inventory.js";

afterEach(() => vi.useRealTimers());

describe("interrupted observation evidence", () => {
  it("retains a collected HTTP404 when its body size never settles", async () => {
    vi.useFakeTimers();
    const context = new EventEmitter();
    const observed = observeContextRequests(context);
    const request = {
      url: () => "https://example.test/missing.js",
      resourceType: () => "script",
      response: async () => ({ status: () => 404 }),
      sizes: () => new Promise(() => {}),
    };
    context.emit("request", request);
    context.emit("response", { request: () => request, status: () => 404 });
    context.emit("requestfinished", request);
    const result = { error: "Execution context was destroyed" };
    const done = ensureRequestInventory(
      result,
      observed.values(),
      [{ url: request.url(), type: "Script", status: 404, complete: true }],
      [],
      "error",
      10,
    );
    await vi.advanceTimersByTimeAsync(10);
    await done;
    expect(result.requests[0]).toMatchObject({
      status: 404,
      transferBytes: null,
      sizeError: "request.sizes timed out",
    });
    expect(result.pageTargetRequests[0].status).toBe(404);
    expect(result.httpErrorRequests).toBe(1);
    expect(result.inventoryPhase).toBe("error");
    expect(result.error).toBe("Execution context was destroyed");
  });

  it("keeps pending requests pending and never overwrites a load-cutoff inventory", async () => {
    const pending = {
      url: "/slow",
      type: "Fetch",
      status: 200,
      complete: false,
      transferBytes: null,
    };
    const result = {};
    await ensureRequestInventory(
      result,
      [pending],
      [{ ...pending }],
      [],
      "load-cutoff",
    );
    pending.complete = true;
    pending.transferBytes = 5000;
    await ensureRequestInventory(result, [pending], [], [], "error");
    expect(result.inventoryPhase).toBe("load-cutoff");
    expect(result.requests[0].complete).toBe(false);
    expect(result.pageTargetRequests[0].complete).toBe(false);
    expect(result.incompleteRequests).toBe(1);
    expect(result.transferBytes).toBe(null);
  });

  it("retains failed request statuses and rejects cleanup that exceeds its bound", async () => {
    vi.useFakeTimers();
    const context = new EventEmitter();
    const observed = observeContextRequests(context);
    const request = {
      url: () => "/missing.js",
      resourceType: () => "script",
      failure: () => ({ errorText: "net::ERR_ABORTED" }),
    };
    context.emit("request", request);
    context.emit("response", { request: () => request, status: () => 404 });
    context.emit("requestfailed", request);
    const result = {};
    await ensureRequestInventory(result, observed.values(), [], [], "error");
    expect(result.requests[0]).toMatchObject({
      status: 404,
      failed: "net::ERR_ABORTED",
    });
    expect(result.httpErrorRequests).toBe(1);
    const cleanup = withinDeadline(
      new Promise(() => {}),
      10,
      "cleanup timeout",
    );
    const rejected = expect(cleanup).rejects.toThrow("cleanup timeout");
    await vi.advanceTimersByTimeAsync(10);
    await rejected;
    expect(vi.getTimerCount()).toBe(0);
  });
});
