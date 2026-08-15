import { describe, expect, test } from "vitest";
import {
  generatorLabel,
  parsePatchHunks,
  walkthroughView,
} from "./walkthrough.js";

const CTX = { sessionId: 301, turnSeq: 2 };

function authored(path, additions, deletions, why = null) {
  return {
    type: "authored",
    register: why ? "testimony" : "fact",
    file_path: path,
    file_change: { additions, deletions, status: "modified" },
    ...(why
      ? { testimony: { turn: 2, attempt: 1, points: [{ path, why }] } }
      : {}),
  };
}

const fullPayload = {
  rung: 1,
  ephemeral: false,
  summary: {
    status: "available",
    files_changed: 146,
    insertions: 100,
    deletions: 16,
    accounted_files: 2,
    unexplained_files: 1,
  },
  steps: [
    authored("swarm/policy.py", 46, 5, "routes on branch movement"),
    authored("swarm/workflows.py", 42, 8, "keeps the observed head"),
    authored("swarm/queues.py", 12, 3),
    {
      type: "mechanical",
      register: "fact",
      count: 143,
      generator_activity: { type: "run", command: "ci regen" },
    },
    {
      type: "unexplained",
      register: "fact",
      file_path: "swarm/queues.py",
      file_change: { additions: 12, deletions: 3, status: "modified" },
      label: "Unexplained file",
    },
    {
      type: "contradiction",
      register: "testimony",
      label: "Contradicted path",
      testimony: {
        turn: 2,
        attempt: 1,
        points: [{ path: "swarm/gone.py", why: "deleted the dead rollup" }],
      },
    },
    { type: "truncation", register: "fact", label: "GitHub files truncated" },
  ],
  stats: { total_files: 146, truncated_at: 300 },
};

describe("walkthroughView rung 1", () => {
  const walk = walkthroughView(fullPayload, CTX);

  test("authored steps become points in the composer's order", () => {
    expect(walk.rung).toBe(1);
    expect(walk.points.map((point) => [point.kind, point.path])).toEqual([
      ["authored", "swarm/policy.py"],
      ["authored", "swarm/workflows.py"],
      ["unexplained", "swarm/queues.py"],
    ]);
    expect(walk.points[0].why).toBe("routes on branch movement");
    expect(walk.points[0].attribution).toEqual({ turn: 2, attempt: 1 });
  });

  test("the authored/unexplained twin pair dedupes to one unexplained point", () => {
    const queues = walk.points.filter(
      (point) => point.path === "swarm/queues.py",
    );
    expect(queues).toHaveLength(1);
    expect(queues[0].kind).toBe("unexplained");
    expect(queues[0].additions).toBe(12);
  });

  test("an authored point with testimony is never dropped by the dedupe", () => {
    const explained = walkthroughView(
      {
        rung: 1,
        steps: [
          authored("a.py", 1, 0, "kept on purpose"),
          {
            type: "unexplained",
            register: "fact",
            file_path: "a.py",
            file_change: { additions: 1, deletions: 0, status: "modified" },
            label: "Unexplained file",
          },
        ],
        stats: { total_files: 1 },
      },
      CTX,
    );
    expect(explained.points.map((point) => point.kind)).toEqual([
      "authored",
      "unexplained",
    ]);
  });

  test("mechanical stays one collapsed row with a count and generator label", () => {
    expect(walk.mechanical).toEqual([{ count: 143, generator: "ci regen" }]);
  });

  test("a contradiction carries the quoted claim and attribution", () => {
    expect(walk.contradictions).toEqual([
      {
        path: "swarm/gone.py",
        why: "deleted the dead rollup",
        attribution: { turn: 2, attempt: 1 },
      },
    ]);
    expect(walk.hasTestimony).toBe(true);
  });

  test("contradictions are counted even when there are no accounted files", () => {
    const contradictions = Array.from({ length: 5 }, (_, index) => ({
      type: "contradiction",
      register: "testimony",
      label: "Contradicted path",
      testimony: {
        turn: 1,
        attempt: 1,
        points: [{ path: `missing-${index}.py`, why: "named by the agent" }],
      },
    }));
    const contradictedOnly = walkthroughView({
      rung: 1,
      steps: contradictions,
    });

    expect(contradictedOnly.counts.accounted).toBe(0);
    expect(contradictedOnly.counts.unexplained).toBe(0);
    expect(contradictedOnly.counts.contradicted).toBe(5);
  });

  test("truncation renders as the server's labels, never silently", () => {
    expect(walk.truncations).toEqual(["GitHub files truncated"]);
  });

  test("counts: explained points, server file total, summed additions", () => {
    expect(walk.counts.points).toBe(2);
    expect(walk.counts.accounted).toBe(2);
    expect(walk.counts.unexplained).toBe(1);
    expect(walk.counts.contradicted).toBe(1);
    expect(walk.counts.touched).toBe(0);
    expect(walk.counts.files).toBe(146);
    expect(walk.counts.additions).toBe(46 + 42 + 12);
    expect(walk.counts.deletions).toBe(5 + 8 + 3);
  });

  test("patches stay lazy: rung 1/2 points link the compare patch route", () => {
    expect(walk.points[0].patchUrl).toBe(
      "/api/swarm/compare/301/2/patch?path=swarm%2Fpolicy.py",
    );
    const uncontexted = walkthroughView(fullPayload, {});
    expect(uncontexted.points[0].patchUrl).toBeNull();
  });

  test("passes through the server-composed factual summary", () => {
    expect(walk.summary).toEqual({
      status: "available",
      files: 146,
      insertions: 100,
      deletions: 16,
      accounted: 2,
      unexplained: 1,
    });
  });
});

describe("walkthroughView degradation ladder", () => {
  test("rung 2 is the same walk with the server's ephemeral message", () => {
    const walk = walkthroughView(
      { ...fullPayload, rung: 2, ephemeral: true, message: "shelf life" },
      CTX,
    );
    expect(walk.rung).toBe(2);
    expect(walk.ephemeral).toBe(true);
    expect(walk.message).toBe("shelf life");
    expect(walk.points[0].patchUrl).toContain("/patch?path=");
  });

  test("rung 3: testimony points with no patch links, touched rows marked", () => {
    const walk = walkthroughView(
      {
        rung: 3,
        ephemeral: false,
        summary: { status: "diff_unavailable" },
        steps: [
          {
            type: "authored",
            register: "testimony",
            file_path: "swarm/rows.py",
            testimony: {
              turn: 2,
              attempt: 1,
              points: [
                { path: "swarm/rows.py", why: "404s a missing turn" },
                { deviation: "left the rollup untouched" },
              ],
            },
          },
          {
            type: "authored",
            register: "fact",
            file_path: "swarm/view.py",
            file_change: { additions: 0, deletions: 0, status: "touched" },
          },
        ],
        stats: { authored_files: 2 },
        message: "Limited walkthrough: testimony and activities only",
      },
      CTX,
    );
    expect(walk.rung).toBe(3);
    expect(walk.points).toHaveLength(2);
    expect(walk.points[0].why).toBe("404s a missing turn");
    expect(walk.points[0].deviations).toEqual(["left the rollup untouched"]);
    expect(walk.points[0].patchUrl).toBeNull();
    expect(walk.points[1].touched).toBe(true);
    expect(walk.counts.accounted).toBe(1);
    expect(walk.counts.touched).toBe(1);
    expect(walk.summary.status).toBe("diff_unavailable");
    expect(walk.message).toContain("Limited walkthrough");
  });

  test("rung 4: no points, the server's decline message, the touched list", () => {
    const activities = Array.from({ length: 23 }, (_, i) => `pkg/m${i}.py`);
    const walk = walkthroughView(
      {
        rung: 4,
        ephemeral: false,
        steps: [],
        stats: { total_files: 23, authored_files: 23, activities },
        message: "decline to offer walkthrough",
      },
      CTX,
    );
    expect(walk.rung).toBe(4);
    expect(walk.points).toEqual([]);
    expect(walk.touched).toHaveLength(23);
    expect(walk.message).toContain("decline");
  });

  test("rung 5: the message is all there is", () => {
    const walk = walkthroughView(
      { rung: 5, ephemeral: false, steps: [], message: "No activity recorded" },
      CTX,
    );
    expect(walk.rung).toBe(5);
    expect(walk.points).toEqual([]);
    expect(walk.mechanical).toEqual([]);
    expect(walk.hasTestimony).toBe(false);
    expect(walk.message).toBe("No activity recorded");
  });
});

describe("generatorLabel", () => {
  test("prefers the command, truncated", () => {
    expect(generatorLabel({ type: "run", command: "ci regen" })).toBe(
      "ci regen",
    );
    const long = "x".repeat(120);
    expect(generatorLabel({ command: long })).toHaveLength(81);
  });

  test("falls back to input then type, and survives junk", () => {
    expect(generatorLabel({ type: "run", input: { cmd: "make" } })).toBe(
      '{"cmd":"make"}',
    );
    expect(generatorLabel({ type: "run" })).toBe("run");
    expect(generatorLabel(null)).toBe("");
    expect(generatorLabel("run")).toBe("");
  });
});

describe("parsePatchHunks", () => {
  test("splits hunks on @@ and types lines with gutters", () => {
    const hunks = parsePatchHunks(
      "@@ -1,3 +1,4 @@\n ctx line\n-old\n+new\n@@ -9,2 +10,2 @@\n+tail",
    );
    expect(hunks).toHaveLength(2);
    expect(hunks[0].header).toBe("@@ -1,3 +1,4 @@");
    expect(hunks[0].lines.map((line) => line.kind)).toEqual([
      "ctx",
      "del",
      "add",
    ]);
    expect(hunks[0].lines[0]).toEqual({
      kind: "ctx",
      gutter: " ",
      text: "ctx line",
    });
    expect(hunks[1].lines).toEqual([
      { kind: "add", gutter: "+", text: "tail" },
    ]);
  });

  test("empty or missing patches parse to nothing", () => {
    expect(parsePatchHunks("")).toEqual([]);
    expect(parsePatchHunks(null)).toEqual([]);
    expect(parsePatchHunks(undefined)).toEqual([]);
  });
});
