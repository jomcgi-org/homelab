import { describe, it, expect } from "vitest";
import { getCachedGraph, setCachedGraph } from "./graph-cache.js";
describe("parsed graph lifetime", () => {
  it("reuses a fresh graph but expires at equality, even on remount", () => {
    const headers = new Headers({
      "cache-control": "public, max-age=13",
      age: "3",
    });
    setCachedGraph({ nodes: [1], edges: [] }, headers, 1000);
    expect(getCachedGraph(10999).nodes).toEqual([1]);
    expect(getCachedGraph(11000)).toBe(null);
  });
  it.each(["no-store", "private, max-age=60", "", "max-age=0"])(
    "does not retain %s",
    (policy) => {
      setCachedGraph(
        { nodes: [1] },
        new Headers({ "cache-control": policy }),
        1000,
      );
      expect(getCachedGraph(1000)).toBe(null);
    },
  );
  it("consumes Date age as well as Age", () => {
    setCachedGraph(
      { nodes: [] },
      new Headers({
        "cache-control": "max-age=13",
        date: new Date(0).toUTCString(),
      }),
      12000,
    );
    expect(getCachedGraph(12999)).not.toBe(null);
    expect(getCachedGraph(13000)).toBe(null);
  });
});
