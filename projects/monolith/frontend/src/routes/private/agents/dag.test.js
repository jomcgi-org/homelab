import { describe, expect, test } from "vitest";
import { capacityPips, computeRanks, nodeIconKey } from "./dag.js";

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
  test("blocked icon silhouettes distinguish human and dependency", () => {
    expect(
      nodeIconKey({ state: "blocked", blocked_on: { kind: "human" } }),
    ).toBe("blocked_human");
    expect(
      nodeIconKey({ state: "blocked", blocked_on: { kind: "dependency" } }),
    ).toBe("blocked_dep");
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
