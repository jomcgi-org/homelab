import { describe, expect, it } from "vitest";
import { fileFor, parseDiff, splitPath } from "./diff.js";

const DIFF = [
  "diff --git a/one.py b/one.py",
  "index 1111111..2222222 100644",
  "--- a/one.py",
  "+++ b/one.py",
  "@@ -1,3 +1,4 @@ def main():",
  " keep",
  "-old",
  "+new",
  "+also added",
  " tail",
  "diff --git a/two.py b/two.py",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/two.py",
  "@@ -0,0 +1 @@",
  "+only",
  "\\ No newline at end of file",
  "diff --git a/img.png b/img.png",
  "Binary files a/img.png and b/img.png differ",
  "",
].join("\n");

describe("parseDiff", () => {
  it("splits files, counts their lines and numbers both sides", () => {
    const parsed = parseDiff(DIFF);
    expect(parsed.files.map((file) => file.path)).toEqual([
      "one.py",
      "two.py",
      "img.png",
    ]);
    expect(parsed.additions).toBe(3);
    expect(parsed.deletions).toBe(1);
    const [one, two, img] = parsed.files;
    expect(one.kind).toBe("modified");
    expect(one.hunks[0].header).toBe("@@ -1,3 +1,4 @@ def main():");
    expect(one.hunks[0].lines).toEqual([
      { kind: "ctx", old: 1, new: 1, text: "keep" },
      { kind: "del", old: 2, new: null, text: "old" },
      { kind: "add", old: null, new: 2, text: "new" },
      { kind: "add", old: null, new: 3, text: "also added" },
      { kind: "ctx", old: 3, new: 4, text: "tail" },
    ]);
    expect(two.kind).toBe("added");
    expect(two.hunks[0].lines).toEqual([
      { kind: "add", old: null, new: 1, text: "only" },
      {
        kind: "note",
        old: null,
        new: null,
        text: "\\ No newline at end of file",
      },
    ]);
    expect(img.kind).toBe("binary");
    expect(img.hunks).toEqual([]);
  });

  it("marks deletions and renames", () => {
    const parsed = parseDiff(
      "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\ndiff --git a/x b/y\nsimilarity index 100%\nrename from x\nrename to y\n",
    );
    expect(parsed.files[0].kind).toBe("deleted");
    expect(parsed.files[0].deletions).toBe(1);
    expect(parsed.files[1]).toMatchObject({
      path: "y",
      oldPath: "x",
      kind: "renamed",
    });
  });

  it("reads a bare hunk with no file header as one unnamed file", () => {
    const parsed = parseDiff("@@ -1 +1 @@\n-a\n+b\n");
    expect(parsed.files).toHaveLength(1);
    expect(parsed.files[0].path).toBe("");
    expect(parsed.files[0].hunks[0].lines.map((line) => line.kind)).toEqual([
      "del",
      "add",
    ]);
  });

  it("returns an empty parse for no diff", () => {
    expect(parseDiff(null)).toEqual({ files: [], additions: 0, deletions: 0 });
    expect(parseDiff("")).toEqual({ files: [], additions: 0, deletions: 0 });
  });

  it("survives a diff clipped in the middle of a hunk", () => {
    const parsed = parseDiff(DIFF.slice(0, 122));
    expect(parsed.files).toHaveLength(1);
    expect(parsed.files[0].hunks[0].lines.length).toBeGreaterThan(0);
  });
});

describe("fileFor", () => {
  const parsed = parseDiff(DIFF);

  it("matches an exact path and a workspace-absolute one", () => {
    expect(fileFor(parsed, "two.py")?.path).toBe("two.py");
    expect(fileFor(parsed, "/workspace/wt/two.py")?.path).toBe("two.py");
    expect(fileFor(parsed, "./one.py")?.path).toBe("one.py");
  });

  it("finds nothing for a path outside the diff", () => {
    expect(fileFor(parsed, "three.py")).toBeNull();
    expect(fileFor(parsed, null)).toBeNull();
    expect(fileFor(null, "two.py")).toBeNull();
  });
});

describe("splitPath", () => {
  it("separates the directory from the name", () => {
    expect(splitPath("projects/monolith/engine.py")).toEqual({
      dir: "projects/monolith/",
      name: "engine.py",
    });
    expect(splitPath("engine.py")).toEqual({ dir: "", name: "engine.py" });
    expect(splitPath(null)).toEqual({ dir: "", name: "" });
  });
});
