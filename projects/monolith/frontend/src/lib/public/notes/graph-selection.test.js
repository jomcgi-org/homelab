import { describe, it, expect } from "vitest";
import { initialGraphSelection, selectionForFocus } from "./graph-selection.js";

describe("graph selection", () => {
  it("never auto-selects a node on a fresh graph", () => {
    expect(initialGraphSelection()).toBeNull();
  });

  it("leaves the panel empty when the graph opens without a focus id", () => {
    expect(selectionForFocus(null)).toBeNull();
    expect(selectionForFocus(undefined)).toBeNull();
  });

  it("selects the focused node when a deep link passes its id", () => {
    expect(selectionForFocus("note-a")).toBe("note-a");
    expect(selectionForFocus(42)).toBe(42);
  });
});
