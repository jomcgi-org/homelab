// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { RUN_LEXICON as P } from "./run-lexicon.js";
import SessionWalkthrough from "./SessionWalkthrough.svelte";
import WalkthroughNarrative from "./WalkthroughNarrative.svelte";

const mounted = [];
const explainedPath = "projects/monolith/swarm/unified_diff.py";
const explanation =
  "Added a one-line comment clarifying that parsing uses guest-captured diff text.";

function narrativeFixture(steps, patches = {}) {
  const unexplained = steps.filter(
    (step) => step.type === "unexplained",
  ).length;
  const contradicted = steps.filter(
    (step) => step.type === "contradiction",
  ).length;
  return {
    model: "sonnet",
    payload: {
      rung: 1,
      summary: {
        status: "available",
        files_changed: steps.filter((step) => step.file_change).length,
        insertions: 1,
        deletions: 0,
        accounted_files: unexplained === 0 ? 1 : 0,
        unexplained_files: unexplained,
        contradicted_files: contradicted,
      },
      steps,
      stats: { total_files: steps.length },
    },
    patches,
  };
}

function explainedFixture() {
  return narrativeFixture(
    [
      {
        type: "authored",
        file_path: explainedPath,
        file_change: { additions: 1, deletions: 0 },
        testimony: {
          turn: 1,
          attempt: 1,
          points: [{ path: explainedPath, why: explanation }],
        },
      },
    ],
    {
      [explainedPath]:
        "@@ -32,6 +32,7 @@\n+# Parses diff text already captured in the guest\n def parse_unified_diff(diff: str) -> list[dict]:",
    },
  );
}

async function render(Component, props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Component, { target, props });
  mounted.push({ component, target });
  await tick();
  return target;
}

afterEach(async () => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("conversation disclosure", () => {
  test("stays collapsed and does not fetch until opened", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ rung: 5, steps: [], stats: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const target = await render(SessionWalkthrough, {
      sessionId: 17,
      turnSeq: 3,
      model: "luna",
    });
    const details = target.querySelector("details");

    expect(details.open).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();

    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    details.dispatchEvent(new Event("toggle"));
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("full-page narrative", () => {
  test("loads once on mount because it renders open", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        rung: 1,
        summary: {
          status: "available",
          files_changed: 1,
          insertions: 1,
          deletions: 0,
          accounted_files: 1,
          unexplained_files: 0,
        },
        steps: [],
        stats: { total_files: 1 },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const target = await render(WalkthroughNarrative, {
      sessionId: 17,
      turnSeq: 3,
      model: "luna",
    });

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(target.textContent).not.toContain("file changed with");
  });

  test("hides the turn label for one walkthrough and shows it for many", async () => {
    const single = await render(WalkthroughNarrative, {
      turnSeq: 1,
      fixture: explainedFixture(),
    });
    const multiple = await render(WalkthroughNarrative, {
      turnSeq: 2,
      walkthroughTurnCount: 2,
      fixture: explainedFixture(),
    });

    expect(single.querySelector(".walk-turn > h3")).toBeNull();
    expect(
      multiple
        .querySelector(".walk-turn > h3")
        ?.textContent.replace(/\s+/g, " ")
        .trim(),
    ).toBe("turn 2");
  });

  test("leads each file with the model prose and leaves out zero accounting", async () => {
    const target = await render(WalkthroughNarrative, {
      turnSeq: 1,
      fixture: explainedFixture(),
    });
    const file = target.querySelector(".file-change");
    const text = file.textContent.replace(/\s+/g, " ").trim();

    expect(text.indexOf(explanation)).toBeLessThan(text.indexOf(explainedPath));
    expect(text.indexOf(explainedPath)).toBeLessThan(text.indexOf("@@ -32"));
    expect(file.querySelector(".hunks")).not.toBeNull();
    expect(target.querySelector(".unexplained-files")).toBeNull();
    expect(target.querySelector(".contradictions")).toBeNull();
    expect(target.textContent).not.toContain(P.labels.walkUnexplainedFilesLine);
    expect(target.textContent).not.toContain(
      P.labels.walkContradictedFilesLine,
    );
    expect(target.textContent).not.toContain("file changed");
    expect(target.textContent).not.toContain("accounted for");
    expect(target.textContent.toLowerCase()).not.toContain("agent-authored");
    expect(target.textContent.toLowerCase()).not.toContain("attempt");
    expect(target.textContent.toLowerCase()).not.toContain("sonnet");
  });

  test("names every file changed without an explanation", async () => {
    const first = "projects/monolith/swarm/queues.py";
    const second = "projects/monolith/swarm/compare.py";
    const unexplainedStep = (path) => ({
      type: "unexplained",
      file_path: path,
      file_change: { additions: 1, deletions: 0 },
    });
    const target = await render(WalkthroughNarrative, {
      turnSeq: 1,
      fixture: narrativeFixture([
        unexplainedStep(first),
        unexplainedStep(second),
      ]),
    });
    const alert = target.querySelector(".unexplained-files .accounting-alert");

    expect(alert.textContent).toContain(P.labels.walkUnexplainedFilesLine);
    expect(alert.textContent).toContain(first);
    expect(alert.textContent).toContain(second);
  });
});
