import { describe, it, expect } from "vitest";
import { buildSectionTree } from "./section-tree.js";

describe("buildSectionTree", () => {
  it("returns an empty array for an empty list", () => {
    expect(buildSectionTree([])).toEqual([]);
  });

  it("treats a path with no slash as a top-level leaf", () => {
    const flat = [{ section_path: "Introduction", title: "Introduction" }];
    const tree = buildSectionTree(flat);
    expect(tree).toEqual([
      {
        title: "Introduction",
        section: flat[0],
        children: [],
      },
    ]);
  });

  it("nests a two-level path under its chapter", () => {
    const flat = [
      { section_path: "Chapter 1/Traps", title: "Traps" },
      { section_path: "Chapter 1/Doors", title: "Doors" },
    ];
    const tree = buildSectionTree(flat);
    expect(tree).toEqual([
      {
        title: "Chapter 1",
        section: null,
        children: [
          { title: "Traps", section: flat[0], children: [] },
          { title: "Doors", section: flat[1], children: [] },
        ],
      },
    ]);
  });

  it("splits only on the FIRST slash, so a slash-bearing leaf title stays intact", () => {
    const flat = [
      { section_path: "Chapter 1/Traps/Pits", title: "Traps/Pits" },
    ];
    const tree = buildSectionTree(flat);
    expect(tree).toEqual([
      {
        title: "Chapter 1",
        section: null,
        children: [{ title: "Traps/Pits", section: flat[0], children: [] }],
      },
    ]);
  });

  it("merges consecutive rows that share the same chapter into one node", () => {
    const flat = [
      { section_path: "Chapter 1/Traps", title: "Traps" },
      { section_path: "Chapter 1/Doors", title: "Doors" },
      { section_path: "Chapter 1/Locks", title: "Locks" },
    ];
    const tree = buildSectionTree(flat);
    expect(tree).toHaveLength(1);
    expect(tree[0].title).toBe("Chapter 1");
    expect(tree[0].children).toHaveLength(3);
  });

  it("keeps non-consecutive duplicate chapter names as separate nodes (reading order wins)", () => {
    const flat = [
      { section_path: "Chapter 1/Traps", title: "Traps" },
      { section_path: "Chapter 2/Intro", title: "Intro" },
      { section_path: "Chapter 1/Doors", title: "Doors" },
    ];
    const tree = buildSectionTree(flat);
    expect(tree).toHaveLength(3);
    expect(tree.map((n) => n.title)).toEqual([
      "Chapter 1",
      "Chapter 2",
      "Chapter 1",
    ]);
    expect(tree[0].children).toEqual([
      { title: "Traps", section: flat[0], children: [] },
    ]);
    expect(tree[2].children).toEqual([
      { title: "Doors", section: flat[2], children: [] },
    ]);
  });

  it("keeps reading order across a mix of top-level leaves and chapters", () => {
    const flat = [
      { section_path: "Foreword", title: "Foreword" },
      { section_path: "Chapter 1/Traps", title: "Traps" },
      { section_path: "Afterword", title: "Afterword" },
    ];
    const tree = buildSectionTree(flat);
    expect(tree.map((n) => n.title)).toEqual([
      "Foreword",
      "Chapter 1",
      "Afterword",
    ]);
    expect(tree[0].section).toBe(flat[0]);
    expect(tree[0].children).toEqual([]);
    expect(tree[2].section).toBe(flat[2]);
  });

  it("handles a single flat section list with no nesting at all", () => {
    const flat = [{ section_path: "Only Section", title: "Only Section" }];
    expect(buildSectionTree(flat)).toEqual([
      { title: "Only Section", section: flat[0], children: [] },
    ]);
  });

  it("treats a null/empty section_path as a top-level leaf", () => {
    const flat = [{ section_path: null, title: "(no section)" }];
    const tree = buildSectionTree(flat);
    expect(tree).toEqual([
      { title: "(no section)", section: flat[0], children: [] },
    ]);
  });
});
