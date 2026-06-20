import { describe, it, expect } from "vitest";
import { readDrJobsParams, writeDrJobsParams } from "./urlParams.js";

function params(query) {
  return new URLSearchParams(query);
}

describe("readDrJobsParams", () => {
  it("defaults to live / no town with no params", () => {
    expect(readDrJobsParams(params(""))).toEqual({ view: "live", town: "" });
  });

  it("reads view and town", () => {
    expect(readDrJobsParams(params("view=history&town=Glasgow"))).toEqual({
      view: "history",
      town: "Glasgow",
    });
  });

  it("falls back to live for an invalid view", () => {
    expect(readDrJobsParams(params("view=archived")).view).toBe("live");
  });
});

describe("writeDrJobsParams", () => {
  it("omits view and town at their defaults", () => {
    const sp = params("");
    writeDrJobsParams(sp, { view: "live", town: "" });
    expect(sp.toString()).toBe("");
  });

  it("writes a non-default view and town", () => {
    const sp = params("");
    writeDrJobsParams(sp, { view: "history", town: "Inverness" });
    expect(sp.get("view")).toBe("history");
    expect(sp.get("town")).toBe("Inverness");
  });

  it("clears the town when it resets to empty", () => {
    const sp = params("view=history&town=Glasgow");
    writeDrJobsParams(sp, { view: "history", town: "" });
    expect(sp.has("town")).toBe(false);
    expect(sp.get("view")).toBe("history");
  });

  it("round-trips through read", () => {
    const state = { view: "history", town: "Aberdeen" };
    const sp = params("");
    writeDrJobsParams(sp, state);
    expect(readDrJobsParams(sp)).toEqual(state);
  });
});
