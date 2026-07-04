import { describe, it, expect } from "vitest";
import { renderChunk } from "./renderChunk.js";

describe("renderChunk (public)", () => {
  it("returns an empty array for empty or missing content", () => {
    expect(renderChunk("")).toEqual([]);
    expect(renderChunk(null)).toEqual([]);
    expect(renderChunk(undefined)).toEqual([]);
  });

  it("joins consecutive prose lines into one paragraph", () => {
    const out = renderChunk("A dragon sleeps here.\nIts hoard glitters below.");
    expect(out).toEqual([
      { type: "para", text: "A dragon sleeps here. Its hoard glitters below." },
    ]);
  });

  it("keeps blank-line-separated prose as distinct paragraphs", () => {
    const out = renderChunk("First para.\n\nSecond para.");
    expect(out).toEqual([
      { type: "para", text: "First para." },
      { type: "para", text: "Second para." },
    ]);
  });

  it("groups consecutive single-newline bullets into one list", () => {
    const out = renderChunk(
      "• darkvision\n• blindsight\n- keen smell\n* pack tactics",
    );
    expect(out).toEqual([
      {
        type: "list",
        items: ["darkvision", "blindsight", "keen smell", "pack tactics"],
      },
    ]);
  });

  it("treats a short ALL-CAPS line as a heading", () => {
    const out = renderChunk("THE UNDERDARK");
    expect(out).toEqual([{ type: "heading", text: "THE UNDERDARK" }]);
  });

  it("does not treat a long ALL-CAPS line as a heading", () => {
    const long =
      "THIS IS A VERY LONG SHOUTED SENTENCE THAT RUNS WELL PAST SIXTY CHARACTERS";
    expect(long.length).toBeGreaterThanOrEqual(60);
    expect(renderChunk(long)).toEqual([{ type: "para", text: long }]);
  });

  it("parses the Monster Manual DUNGEONS chunk into heading, prose, list, heading", () => {
    // Reproduces the seq-9 failure: single-newline bullets and inline ALL-CAPS
    // headings that a naive content.split(/\n\n+/) flattened into run-on prose.
    const content = [
      "DUNGEONS",
      "Dungeons are the classic adventuring site.",
      "They hold traps and treasure alike.",
      "• a crumbling stair",
      "• a flooded vault",
      "THE UNDERDARK",
      "Below it all lies the Underdark.",
    ].join("\n");

    expect(renderChunk(content)).toEqual([
      { type: "heading", text: "DUNGEONS" },
      {
        type: "para",
        text: "Dungeons are the classic adventuring site. They hold traps and treasure alike.",
      },
      { type: "list", items: ["a crumbling stair", "a flooded vault"] },
      { type: "heading", text: "THE UNDERDARK" },
      { type: "para", text: "Below it all lies the Underdark." },
    ]);
  });
});
