import { describe, expect, it } from "vitest";

import { modelEntries, modelLabel, modelName } from "./model-list.js";

describe("modelEntries", () => {
  it("passes through the catalogue the endpoint returned", () => {
    const entries = [
      { name: "luna", family: "codex" },
      { name: "qwen", family: "pi" },
    ];

    expect(modelEntries({ models: entries })).toEqual(entries);
  });

  it("accepts bare-string entries", () => {
    expect(modelEntries({ models: ["qwen"] })).toEqual(["qwen"]);
  });

  it("renders an empty response as an EMPTY catalogue, never a fallback", () => {
    expect(modelEntries({ models: [] })).toEqual([]);
  });

  it("treats a missing or malformed list as empty", () => {
    expect(modelEntries(undefined)).toEqual([]);
    expect(modelEntries({})).toEqual([]);
    expect(modelEntries({ models: "qwen" })).toEqual([]);
  });

  it("drops entries without a usable name", () => {
    expect(
      modelEntries({
        models: [{ name: "luna", family: "codex" }, null, { family: "pi" }, {}],
      }),
    ).toEqual([{ name: "luna", family: "codex" }]);
  });
});

describe("modelName / modelLabel", () => {
  it("reads the name from object and string entries alike", () => {
    expect(modelName({ name: "luna" })).toBe("luna");
    expect(modelName("luna")).toBe("luna");
  });

  it("labels only the free in-cluster pi lane as local", () => {
    expect(modelLabel({ name: "qwen", family: "pi" })).toBe("qwen (local)");
    expect(modelLabel({ name: "opus", family: "claude" })).toBe("opus");
    expect(modelLabel({ name: "sol" })).toBe("sol");
  });

  it("labels a nameless entry as empty rather than undefined", () => {
    expect(modelLabel({})).toBe("");
  });
});
