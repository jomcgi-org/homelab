import { describe, expect, test } from "vitest";
import {
  capacityPips,
  computeRanks,
  defaultSelectedKey,
  layoutEdges,
  nodeIconKey,
  nodeStateClass,
} from "./dag.js";

const chain = [
  { key: "a", deps: [] },
  { key: "b", deps: ["a"] },
  { key: "c", deps: ["b"] },
];
const diamond = [
  { key: "a", deps: [] },
  { key: "b", deps: ["a"] },
  { key: "c", deps: ["a"] },
  { key: "fx9", deps: ["b", "c"] },
  { key: "slice-1", deps: ["fx9"] },
  { key: "slice-2", deps: ["fx9"] },
  { key: "slice-3", deps: ["fx9"] },
];

describe("run DAG helpers", () => {
  test("chain ranks are singletons", () =>
    expect(computeRanks(chain).map((rank) => rank.length)).toEqual([1, 1, 1]));
  test("diamond fixture groups fx9's three slices in one rank", () =>
    expect(
      computeRanks(diamond)
        .at(-1)
        .map((node) => node.key),
    ).toEqual(["slice-1", "slice-2", "slice-3"]));
  test("layout edges match rank pairs and reflect upstream completion", () => {
    const ranks = computeRanks([
      { key: "a", deps: [], state: "done" },
      { key: "b", deps: ["a"], state: "passed" },
      { key: "c", deps: ["b"], state: "running" },
      { key: "d", deps: ["c"], state: "future" },
    ]);
    expect(layoutEdges(ranks)).toEqual([
      { dim: false, strong: true },
      { dim: false, strong: true },
      { dim: true, strong: false },
    ]);
    expect(layoutEdges(ranks)).toHaveLength(ranks.length - 1);
  });
  test("default selection prefers attention, running, then the last done", () => {
    expect(
      defaultSelectedKey([
        { key: "done", state: "done" },
        { key: "running", state: "running" },
        { key: "escalated", state: "escalated" },
      ]),
    ).toBe("escalated");
    expect(
      defaultSelectedKey([
        { key: "done", state: "done" },
        { key: "running", state: "running" },
        { key: "human", state: "blocked", blocked_on: { kind: "human" } },
      ]),
    ).toBe("human");
    expect(
      defaultSelectedKey([
        { key: "done", state: "done" },
        { key: "running", state: "running" },
      ]),
    ).toBe("running");
    expect(
      defaultSelectedKey([
        { key: "first", state: "done" },
        { key: "last", state: "done" },
        { key: "future", state: "future" },
      ]),
    ).toBe("last");
  });
  test("blocked icon silhouettes distinguish human and dependency", () => {
    const human = { state: "blocked", blocked_on: { kind: "human" } };
    const dependency = {
      state: "blocked",
      blocked_on: { kind: "dependency" },
    };
    expect([nodeIconKey(human), nodeStateClass(human)]).toEqual([
      "blocked_human",
      "g-blocked-h",
    ]);
    expect([nodeIconKey(dependency), nodeStateClass(dependency)]).toEqual([
      "blocked_dep",
      "g-blocked-d",
    ]);
  });
  test.each([
    ["done", "done", "g-done"],
    ["completed", "done", "g-done"],
    ["running", "running", "g-running pulse"],
    ["working", "running", "g-running pulse"],
    ["reviewing", "running", "g-running pulse"],
    ["queued", "queued", "g-queued"],
    ["future", "future", "g-future"],
    ["escalated", "escalated", "g-escalated"],
    ["needs_input", "escalated", "g-blocked-h"],
    ["stranded", "escalated", "g-blocked-h"],
    ["changes_requested", "escalated", "g-blocked-h"],
    ["failed", "failed", "g-failed"],
    ["warn", "failed", "g-failed"],
    ["cancelled", "cancelled", "g-cancelled"],
    ["waiting", "gate", "g-waiting"],
    ["refused", "gate", "g-refused"],
  ])("%s maps to the %s icon and %s class", (state, iconKey, stateClass) => {
    const node = { state };
    expect(nodeIconKey(node)).toBe(iconKey);
    expect(nodeStateClass(node)).toBe(stateClass);
  });
  test("expansion nodes use the expansion icon", () =>
    expect(nodeIconKey({ kind: "expansion", state: "future" })).toBe(
      "expansion",
    ));
  test("pinned capacity adds one free pip", () =>
    expect(
      capacityPips(
        { pinned: true, max_attempts: 2 },
        { attempts: [{ state: "done" }] },
      ),
    ).toEqual(["pip", "pip free"]));
  test("pinned capacity does not add free pips when all are spent", () =>
    expect(
      capacityPips(
        { pinned: true, max_attempts: 2 },
        { attempts: [{ state: "done" }, { state: "failed" }] },
      ),
    ).toEqual(["pip", "pip bad"]));
  test("pinned running attempt keeps its running pip", () =>
    expect(
      capacityPips(
        { pinned: true, max_attempts: 2 },
        { attempts: [{ state: "running" }] },
      ),
    ).toEqual(["pip run", "pip free"]));
  test("unpinned capacity only shows spent pips", () =>
    expect(
      capacityPips(
        { pinned: false, max_attempts: 2 },
        { attempts: [{ state: "done" }, { state: "failed" }] },
      ),
    ).toEqual(["pip", "pip bad"]));
  test("missing max attempts does not add free pips", () =>
    expect(
      capacityPips({ pinned: true }, { attempts: [{ state: "done" }] }),
    ).toEqual(["pip"]));
  test("spent attempts beyond capacity do not produce negative pips", () =>
    expect(
      capacityPips(
        { pinned: true, max_attempts: 1 },
        { attempts: [{ state: "done" }, { state: "failed" }] },
      ),
    ).toEqual(["pip", "pip bad"]));
});
