import { describe, it, expect } from "vitest";
import { buildSectionTree } from "./section-tree.js";

// Rows mirror the backend's shape: one row per distinct breadcrumb node,
// carrying section_path (the full " > "-joined breadcrumb), title (the
// node's own last segment), depth, and parent_path (the parent's
// section_path, or null at depth 0).
function row(section_path, title, depth, parent_path = null) {
  return {
    section_path,
    title,
    depth,
    parent_path,
    first_chunk_id: "c-" + title,
  };
}

describe("buildSectionTree", () => {
  it("returns an empty array for an empty list", () => {
    expect(buildSectionTree([])).toEqual([]);
  });

  it("treats a depth-0 row with no children as a top-level leaf", () => {
    const flat = [row("Introduction", "Introduction", 0)];
    const tree = buildSectionTree(flat);
    expect(tree).toEqual([
      { title: "Introduction", section: flat[0], children: [] },
    ]);
  });

  it("nests a depth-1 row under its depth-0 parent", () => {
    const flat = [
      row("Chapter 1", "Chapter 1", 0),
      row("Chapter 1 > Traps", "Traps", 1, "Chapter 1"),
      row("Chapter 1 > Doors", "Doors", 1, "Chapter 1"),
    ];
    const tree = buildSectionTree(flat);
    expect(tree).toHaveLength(1);
    expect(tree[0].title).toBe("Chapter 1");
    expect(tree[0].children).toEqual([
      { title: "Traps", section: flat[1], children: [] },
      { title: "Doors", section: flat[2], children: [] },
    ]);
  });

  it("nests three levels deep", () => {
    const flat = [
      row("Chapter 1", "Chapter 1", 0),
      row("Chapter 1 > Armor", "Armor", 1, "Chapter 1"),
      row(
        "Chapter 1 > Armor > Armor of Vulnerability",
        "Armor of Vulnerability",
        2,
        "Chapter 1 > Armor",
      ),
    ];
    const tree = buildSectionTree(flat);
    expect(tree).toHaveLength(1);
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children[0].title).toBe("Armor");
    expect(tree[0].children[0].children).toEqual([
      { title: "Armor of Vulnerability", section: flat[2], children: [] },
    ]);
  });

  it("keeps reading order across a mix of top-level leaves and chapters", () => {
    const flat = [
      row("Foreword", "Foreword", 0),
      row("Chapter 1", "Chapter 1", 0),
      row("Chapter 1 > Traps", "Traps", 1, "Chapter 1"),
      row("Afterword", "Afterword", 0),
    ];
    const tree = buildSectionTree(flat);
    expect(tree.map((n) => n.title)).toEqual([
      "Foreword",
      "Chapter 1",
      "Afterword",
    ]);
    expect(tree[0].section).toBe(flat[0]);
    expect(tree[0].children).toEqual([]);
    expect(tree[2].section).toBe(flat[3]);
  });

  it("handles a single flat section list with no nesting at all", () => {
    const flat = [row("Only Section", "Only Section", 0)];
    expect(buildSectionTree(flat)).toEqual([
      { title: "Only Section", section: flat[0], children: [] },
    ]);
  });

  it("treats the (no section) placeholder as a depth-0 leaf", () => {
    const flat = [row("(no section)", "(no section)", 0)];
    const tree = buildSectionTree(flat);
    expect(tree).toEqual([
      { title: "(no section)", section: flat[0], children: [] },
    ]);
  });
});
