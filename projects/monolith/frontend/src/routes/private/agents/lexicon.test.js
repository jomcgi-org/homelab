import { describe, expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { RUN_LEXICON } from "./run-lexicon.js";

// Read via import.meta.url rather than a path relative to the working
// directory: the bazel test runner chdirs into the package, vitest run
// locally does not, and a test that only passes under one of them is worse
// than no test.
const surface = (name) =>
  readFileSync(new URL(`./${name}`, import.meta.url), "utf8");

const SURFACES = [
  "+page.svelte",
  "RunView.svelte",
  "MasterView.svelte",
  "PaneHeader.svelte",
  "SessionWalkthrough.svelte",
  "WalkthroughNarrative.svelte",
  "JumpPalette.svelte",
];

function allValues(section) {
  return Object.values(section).filter((value) => typeof value === "string");
}

const EVERY_VALUE = [
  ...allValues(RUN_LEXICON.labels),
  ...allValues(RUN_LEXICON.stateWords),
  ...allValues(RUN_LEXICON.nodeStates),
  ...allValues(RUN_LEXICON.deviationCodes),
];

describe("run lexicon", () => {
  // Centralising copy trades many visible literals for one invisible failure
  // mode: a mistyped key is not an error, it renders as an empty string and
  // compiles clean. Only a check like this one catches it, which is the price
  // of the table existing at all.
  test("every P.labels reference resolves to a defined label", () => {
    const missing = [];
    let scanned = 0;
    for (const name of SURFACES) {
      for (const [, key] of surface(name).matchAll(/\bP\.labels\.(\w+)/g)) {
        scanned += 1;
        if (RUN_LEXICON.labels[key] === undefined)
          missing.push(`${name}: ${key}`);
      }
    }
    expect(missing).toEqual([]);
    // A scan that reads the wrong file, or a rename that moves every call site
    // out from under the regex, finds zero references and passes. Asserting
    // the work happened is the difference between a guard and a green light.
    expect(scanned).toBeGreaterThan(30);
  });

  // Only dot access is checkable. P.stateWords[run.state] and
  // P.deviationCodes[deviation.code] are indexed by server-owned values, and
  // both already fall back to the raw code rather than rendering blank.
  test("every label has a non-empty value", () => {
    const blank = Object.entries(RUN_LEXICON.labels).filter(
      ([, value]) => typeof value !== "string" || value === "",
    );
    expect(blank).toEqual([]);
  });

  test("no lexicon value contains an em-dash", () => {
    expect(EVERY_VALUE.filter((value) => value.includes("—"))).toEqual([]);
  });

  test("planned task input question preserves the task and focuses the repo", () => {
    const page = surface("+page.svelte");
    expect(page).toContain("let needsInputState = $state(false)");
    expect(page).toContain("pendingTaskId ? { task_id: pendingTaskId } : {}");
    expect(page).toContain('body.kind === "needs_input"');
    expect(page).toContain("repoControlEl?.focus");
    expect(page).toContain("P.labels.plannedNeedsRepoBranch");
    expect(page).toContain("class:needs-input={needsInputState}");
  });
});
