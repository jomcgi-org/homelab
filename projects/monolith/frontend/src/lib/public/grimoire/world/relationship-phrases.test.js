import { describe, it, expect } from "vitest";
import {
  phrase,
  humanize,
  isSymmetric,
  SYMMETRIC,
} from "./relationship-phrases.js";

// focusId used throughout: the World codex is always focused on one entity, and
// `phrase` words the edge relative to it.
const FOCUS = 7;

describe("humanize", () => {
  it("lower-cases a single UPPER token", () => {
    expect(humanize("OWNS")).toBe("owns");
  });

  it("spaces and lower-cases a multi-word snake_case rel_type", () => {
    expect(humanize("LOCATED_IN")).toBe("located in");
    expect(humanize("located_in")).toBe("located in");
  });

  it("returns an empty string for null/undefined", () => {
    expect(humanize(null)).toBe("");
    expect(humanize(undefined)).toBe("");
  });
});

describe("isSymmetric", () => {
  it("recognizes canonical symmetric types case-insensitively", () => {
    expect(isSymmetric("ALLY_OF")).toBe(true);
    expect(isSymmetric("ally_of")).toBe(true);
    expect(isSymmetric("SIBLING_OF")).toBe(true);
    expect(isSymmetric("RELATED_TO")).toBe(true);
  });

  it("treats directional types as asymmetric", () => {
    expect(isSymmetric("OWNS")).toBe(false);
    expect(isSymmetric("LOCATED_IN")).toBe(false);
    expect(isSymmetric("CREATES")).toBe(false);
  });

  it("is false for unknown/empty", () => {
    expect(isSymmetric("FROBNICATES")).toBe(false);
    expect(isSymmetric("")).toBe(false);
    expect(isSymmetric(null)).toBe(false);
  });
});

describe("SYMMETRIC set", () => {
  it("contains exactly the shipped symmetric rel_types (lower-cased)", () => {
    expect([...SYMMETRIC].sort()).toEqual(
      [
        "ally_of",
        "associated_with",
        "connects_to",
        "enemy_of",
        "near",
        "related_to",
        "sibling_of",
        "spouse_of",
        "variant_of",
      ].sort(),
    );
  });
});

describe("phrase", () => {
  it("outgoing directional edge leads with the verb: 'owns <peer>'", () => {
    const edge = { from: FOCUS, to: 9, rel_type: "OWNS" };
    expect(phrase({ focusId: FOCUS, edge, peerName: "the Sunsword" })).toEqual({
      pre: "owns ",
      peer: "the Sunsword",
      post: "",
    });
  });

  it("incoming directional edge trails with '<peer> owns this'", () => {
    const edge = { from: 9, to: FOCUS, rel_type: "OWNS" };
    expect(phrase({ focusId: FOCUS, edge, peerName: "Strahd" })).toEqual({
      pre: "",
      peer: "Strahd",
      post: " owns this",
    });
  });

  it("symmetric edge reads the same peer-trailing form when outgoing", () => {
    const edge = { from: FOCUS, to: 9, rel_type: "ALLY_OF" };
    expect(phrase({ focusId: FOCUS, edge, peerName: "the Keepers" })).toEqual({
      pre: "ally of ",
      peer: "the Keepers",
      post: "",
    });
  });

  it("symmetric edge reads the same peer-trailing form when incoming", () => {
    // edge.to === focus (incoming), but symmetric so still 'ally of <peer>',
    // never '<peer> ally of this'.
    const edge = { from: 9, to: FOCUS, rel_type: "ALLY_OF" };
    expect(phrase({ focusId: FOCUS, edge, peerName: "the Keepers" })).toEqual({
      pre: "ally of ",
      peer: "the Keepers",
      post: "",
    });
  });

  it("multi-word directional rel_type: outgoing 'located in <peer>'", () => {
    const edge = { from: FOCUS, to: 9, rel_type: "LOCATED_IN" };
    expect(phrase({ focusId: FOCUS, edge, peerName: "Barovia" })).toEqual({
      pre: "located in ",
      peer: "Barovia",
      post: "",
    });
  });

  it("multi-word directional rel_type: incoming '<peer> located in this'", () => {
    const edge = { from: 9, to: FOCUS, rel_type: "LOCATED_IN" };
    expect(
      phrase({ focusId: FOCUS, edge, peerName: "the Amber Temple" }),
    ).toEqual({
      pre: "",
      peer: "the Amber Temple",
      post: " located in this",
    });
  });

  it("unknown rel_type defaults to directional handling (outgoing)", () => {
    const edge = { from: FOCUS, to: 9, rel_type: "FROBNICATES" };
    expect(phrase({ focusId: FOCUS, edge, peerName: "the Widget" })).toEqual({
      pre: "frobnicates ",
      peer: "the Widget",
      post: "",
    });
  });

  it("unknown rel_type defaults to directional handling (incoming)", () => {
    const edge = { from: 9, to: FOCUS, rel_type: "FROBNICATES" };
    expect(phrase({ focusId: FOCUS, edge, peerName: "the Widget" })).toEqual({
      pre: "",
      peer: "the Widget",
      post: " frobnicates this",
    });
  });

  it("string ids compare equal to a matching string focusId", () => {
    const edge = { from: "e7", to: "e9", rel_type: "OWNS" };
    expect(phrase({ focusId: "e7", edge, peerName: "the Sunsword" })).toEqual({
      pre: "owns ",
      peer: "the Sunsword",
      post: "",
    });
  });

  it("missing peerName degrades to an empty peer, not undefined", () => {
    const edge = { from: FOCUS, to: 9, rel_type: "OWNS" };
    expect(phrase({ focusId: FOCUS, edge })).toEqual({
      pre: "owns ",
      peer: "",
      post: "",
    });
  });
});
