import { describe, expect, test } from "vitest";
import { composeWalkthrough, parsePatch } from "./walkthrough.js";

function file(path, classification, extra = {}) {
  return {
    path,
    status: "modified",
    additions: 4,
    deletions: 1,
    changes: 5,
    classification,
    patch_url:
      classification === "authored"
        ? `/api/swarm/compare/1/2/patch?path=${path}`
        : null,
    ...extra,
  };
}

function rationaleOf(paths, deviations = []) {
  return {
    raw: "RATIONALE",
    parse_status: "parsed",
    paths,
    deviations,
    parser_version: 1,
  };
}

const fullCompare = {
  resolution_rung: 1,
  diff_type: "sha",
  trailer_parsed: true,
  files: [
    file("swarm/rows.py", "authored"),
    file("swarm/view.py", "authored"),
    file("swarm/quiet.py", "authored"),
    file("charts/index.yaml", "mechanical"),
    file("charts/lock.json", "mechanical"),
  ],
  stats: { total_files: 5 },
  authored_file_paths: ["swarm/rows.py", "swarm/view.py", "swarm/quiet.py"],
  unexplained_files: [
    "swarm/quiet.py",
    "charts/index.yaml",
    "charts/lock.json",
  ],
  contradicted_paths: ["swarm/gone.py"],
};

const fullRationale = rationaleOf(
  [
    { path: "swarm/view.py", why: "renders the walkthrough" },
    { path: "swarm/rows.py", why: "carries the final turn" },
    { path: "swarm/gone.py", why: "removed the dead column" },
  ],
  ["kept routing unchanged"],
);

describe("composeWalkthrough rung 1", () => {
  const walk = composeWalkthrough(fullCompare, fullRationale);

  test("authored files become steps in the agent's order first", () => {
    expect(walk.rung).toBe(1);
    expect(walk.steps.map((step) => step.path)).toEqual([
      "swarm/view.py",
      "swarm/rows.py",
      "swarm/quiet.py",
    ]);
    expect(walk.steps[0].why).toBe("renders the walkthrough");
    expect(walk.steps[0].patchUrl).toContain("/patch?path=");
  });

  test("mechanical files collapse to one step with a count, never flagged", () => {
    expect(walk.mechanical).toEqual({
      count: 2,
      files: ["charts/index.yaml", "charts/lock.json"],
    });
    // 5 changed files render as 3 steps plus one mechanical group.
    expect(walk.steps).toHaveLength(3);
  });

  test("an authored file no point mentions is flagged unexplained", () => {
    const quiet = walk.steps.find((step) => step.path === "swarm/quiet.py");
    expect(quiet.unexplained).toBe(true);
    expect(walk.steps.filter((step) => step.unexplained)).toHaveLength(1);
  });

  test("a point naming a file absent from the diff is contradicted, with its claim", () => {
    expect(walk.contradicted).toEqual([
      { path: "swarm/gone.py", why: "removed the dead column" },
    ]);
  });

  test("deviations pass through as testimony", () => {
    expect(walk.deviations).toEqual(["kept routing unchanged"]);
  });

  test("stats sum the whole compare", () => {
    expect(walk.stats).toEqual({ totalFiles: 5, additions: 20, deletions: 5 });
  });
});

describe("composeWalkthrough degradation ladder", () => {
  test("rung 2 is the same walkthrough labelled ephemeral", () => {
    const walk = composeWalkthrough(
      { ...fullCompare, resolution_rung: 2, diff_type: "branch_ephemeral" },
      fullRationale,
    );
    expect(walk.rung).toBe(2);
    expect(walk.ephemeral).toBe(true);
    expect(walk.steps).toHaveLength(3);
  });

  test("rung 3: no compare, trailer parsed: testimony steps, no diff panes, no cross-check", () => {
    const walk = composeWalkthrough(
      {
        resolution_rung: 3,
        trailer_parsed: true,
        files: [],
        stats: { total_files: 0 },
        authored_file_paths: ["swarm/rows.py"],
        unexplained_files: [],
        // Against an empty diff the server set-difference names every
        // trailer path; that is not a contradiction and must not render.
        contradicted_paths: ["swarm/view.py", "swarm/rows.py"],
        error: "no_compare_available",
      },
      rationaleOf([{ path: "swarm/rows.py", why: "carries the final turn" }]),
    );
    expect(walk.rung).toBe(3);
    expect(walk.steps).toEqual([
      {
        path: "swarm/rows.py",
        status: null,
        additions: null,
        deletions: null,
        why: "carries the final turn",
        unexplained: false,
        patchUrl: null,
      },
    ]);
    expect(walk.contradicted).toEqual([]);
    expect(walk.touched).toEqual(["swarm/rows.py"]);
  });

  test("rung 4: no compare, no trailer, files touched: declines to walk", () => {
    const touched = Array.from({ length: 40 }, (_, i) => `pkg/mod_${i}.py`);
    const walk = composeWalkthrough({
      resolution_rung: 3,
      trailer_parsed: false,
      files: [],
      stats: { total_files: 0 },
      authored_file_paths: touched,
      unexplained_files: [],
      contradicted_paths: [],
      error: "no_compare_available",
    });
    expect(walk.rung).toBe(4);
    expect(walk.declined).toBe(true);
    expect(walk.steps).toEqual([]);
    expect(walk.touched).toHaveLength(40);
  });

  test("rung 5: nothing at all", () => {
    const walk = composeWalkthrough({
      resolution_rung: 3,
      trailer_parsed: false,
      files: [],
      stats: { total_files: 0 },
      authored_file_paths: [],
      unexplained_files: [],
      contradicted_paths: [],
      error: "no_compare_available",
    });
    expect(walk.rung).toBe(5);
    expect(walk.declined).toBe(false);
    expect(walk.steps).toEqual([]);
  });
});

describe("composeWalkthrough edge facts", () => {
  test("truncation renders as labelled facts from the server, never silently", () => {
    const walk = composeWalkthrough(
      {
        ...fullCompare,
        stats: {
          total_files: 300,
          truncated_at: 300,
          truncated_reason: "GitHub files array capped at 300",
        },
        activities_truncated: true,
        activities_truncated_reason: "activities ingest capped at 300",
      },
      fullRationale,
    );
    expect(walk.truncation).toEqual([
      "GitHub files array capped at 300",
      "activities ingest capped at 300",
    ]);
  });

  test("a large mechanical run stays one step", () => {
    const mech = Array.from({ length: 143 }, (_, i) =>
      file(`gen/out_${i}.json`, "mechanical"),
    );
    const walk = composeWalkthrough(
      {
        ...fullCompare,
        files: [file("swarm/rows.py", "authored"), ...mech],
        unexplained_files: [],
        contradicted_paths: [],
      },
      rationaleOf([{ path: "swarm/rows.py", why: "carries the final turn" }]),
    );
    expect(walk.steps).toHaveLength(1);
    expect(walk.mechanical.count).toBe(143);
  });

  test("compare without served rationale content: server cross-check still applies", () => {
    // trailer_parsed is a server fact; the content arrives only once the
    // composer serves it. Flags key off the fact, quotes need the content.
    const walk = composeWalkthrough(fullCompare, null);
    expect(walk.rung).toBe(1);
    expect(walk.trailer).toBe(true);
    expect(walk.steps.map((step) => step.path)).toEqual([
      "swarm/rows.py",
      "swarm/view.py",
      "swarm/quiet.py",
    ]);
    const quiet = walk.steps.find((step) => step.path === "swarm/quiet.py");
    expect(quiet.unexplained).toBe(true);
    expect(walk.contradicted).toEqual([{ path: "swarm/gone.py", why: "" }]);
  });

  test("no trailer at all: absence is one fact, not a flag per row", () => {
    const walk = composeWalkthrough(
      {
        ...fullCompare,
        trailer_parsed: false,
        unexplained_files: fullCompare.files.map((f) => f.path),
        contradicted_paths: [],
      },
      null,
    );
    expect(walk.trailer).toBe(false);
    expect(walk.steps.every((step) => step.unexplained === false)).toBe(true);
  });
});

describe("parsePatch", () => {
  test("types hunk, add, del, and context lines", () => {
    const lines = parsePatch(
      "@@ -1,3 +1,4 @@\n context\n-old line\n+new line\n+another",
    );
    expect(lines.map((line) => line.kind)).toEqual([
      "hunk",
      "ctx",
      "del",
      "add",
      "add",
    ]);
    expect(lines[3].text).toBe("+new line");
  });

  test("empty or missing patches parse to nothing", () => {
    expect(parsePatch("")).toEqual([]);
    expect(parsePatch(null)).toEqual([]);
    expect(parsePatch(undefined)).toEqual([]);
  });
});
