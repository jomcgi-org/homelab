import { describe, expect, it } from "vitest";
import { decodeSearchIndex, rankIndexMatches } from "./search-index.js";

// rankIndexMatches takes decoded notes, so the helper decodes here the same
// way the page does once on load.
function indexFor(titles) {
  return decodeSearchIndex({
    states: ["verified", "unverified"],
    entities: ["embervm"],
    notes: titles.map((title, i) => [
      `note-${i}`,
      title,
      i % 2,
      i % 3 === 0 ? 0 : -1,
    ]),
  });
}

describe("rankIndexMatches", () => {
  it("ranks starts-with, then word-boundary, then substring matches", () => {
    const index = indexFor([
      "Remembered detail",
      "Cold Ember",
      "Ember snapshots",
      "An ember detail",
      "Ember agents",
    ]);

    expect(rankIndexMatches(index, "ember").map((note) => note.title)).toEqual([
      "Ember snapshots",
      "Ember agents",
      "Cold Ember",
      "An ember detail",
      "Remembered detail",
    ]);
  });

  it("preserves index order within each ranking band", () => {
    const index = indexFor(["Ember newer", "Ember older", "The Ember"]);

    expect(rankIndexMatches(index, "ember").map((note) => note.title)).toEqual([
      "Ember newer",
      "Ember older",
      "The Ember",
    ]);
  });

  it("caps the result count", () => {
    const index = indexFor(Array.from({ length: 25 }, (_, i) => `Fact ${i}`));

    expect(rankIndexMatches(index, "fact")).toHaveLength(20);
  });

  it("matches case-insensitively and decodes state and entity", () => {
    const [match] = rankIndexMatches(indexFor(["EMBER fact"]), "ember");

    expect(match).toEqual({
      note_id: "note-0",
      title: "EMBER fact",
      search: "ember fact",
      verification_state: "verified",
      entity: "embervm",
    });
  });

  it("returns nothing for an empty query", () => {
    expect(rankIndexMatches(indexFor(["Fact"]), "  ")).toEqual([]);
  });

  it("returns nothing when no title matches", () => {
    expect(rankIndexMatches(indexFor(["Fact"]), "missing")).toEqual([]);
  });
});
