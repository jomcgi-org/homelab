import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";
import {
  sandboxRestoreMs,
  coldGuestWaitMs,
  baseSnapshotBuildSec,
  runCount,
  agentRestoreWarmMs,
  agentRestoreColdMs,
  agentFirstModelCallMs,
  semgrepRestoreMs,
  semgrepScanSec,
  semgrepColdStartSec,
} from "./metrics.js";
import { restores } from "./data/trace.js";

describe("fcstory metrics: derived exports are sane", () => {
  it("sandboxRestoreMs is the rounded mean warm restore, in a plausible range", () => {
    expect(sandboxRestoreMs).toBeGreaterThan(10);
    expect(sandboxRestoreMs).toBeLessThan(100);
    expect(Number.isInteger(sandboxRestoreMs)).toBe(true);
  });

  it("runCount matches the number of baked restore runs (12)", () => {
    expect(runCount).toBe(12);
    expect(runCount).toBe(restores.length);
  });

  it("coldGuestWaitMs and baseSnapshotBuildSec are rounded and plausible", () => {
    expect(Number.isInteger(coldGuestWaitMs)).toBe(true);
    expect(coldGuestWaitMs).toBeGreaterThan(1000);
    expect(baseSnapshotBuildSec).toBeGreaterThan(1);
    expect(baseSnapshotBuildSec).toBeLessThan(60);
  });

  it("agent-platform figures are positive integers", () => {
    for (const v of [
      agentRestoreWarmMs,
      agentRestoreColdMs,
      agentFirstModelCallMs,
    ]) {
      expect(Number.isInteger(v)).toBe(true);
      expect(v).toBeGreaterThan(0);
    }
  });

  it("agentRestoreWarmMs is faster than agentRestoreColdMs", () => {
    expect(agentRestoreWarmMs).toBeLessThan(agentRestoreColdMs);
  });

  it("semgrep scan-guest figures are positive and ordered", () => {
    expect(Number.isInteger(semgrepRestoreMs)).toBe(true);
    expect(semgrepRestoreMs).toBeGreaterThan(0);
    expect(semgrepScanSec).toBeGreaterThan(0);
    // The whole point of the snapshot-warm guest: a warm scan beats cold start.
    expect(semgrepScanSec).toBeLessThan(semgrepColdStartSec);
  });
});

// Drift guard: every ms literal quoted about Firecracker timing in site copy
// must come from this module, not a re-typed number. Checking the *exported
// string content* of engineering-data.js / cv-data.js cannot tell a
// hardcoded "28ms" apart from a correctly interpolated `${agentRestoreColdMs}ms`
// (both render to the string "28ms"), so this checks the SOURCE TEXT instead:
// a bare `<number>ms` literal can only appear in source if nobody used the
// metrics.js import, whereas an interpolated value appears in source as
// `${identifier}ms`, which the regex below does not match. The .svelte call
// sites are intentionally not covered here: their <style> blocks contain
// legitimate CSS durations (e.g. "140ms ease") that are not timing figures.
describe("no hardcoded firecracker ms literals outside metrics.js", () => {
  const readSource = (relPath) =>
    readFileSync(fileURLToPath(new URL(relPath, import.meta.url)), "utf8");

  // Matches a numeric literal directly followed by "ms" with no
  // interpolation between them, e.g. "28ms" or "~140ms", but not
  // "${agentRestoreColdMs}ms".
  const RAW_MS_LITERAL = /\b\d+(\.\d+)?\s?ms\b/g;

  it("engineering-data.js has no raw ms literals in source", () => {
    const src = readSource(
      "../../../routes/public/engineering/engineering-data.js",
    );
    const matches = src.match(RAW_MS_LITERAL) ?? [];
    expect(
      matches,
      `found raw ms literal(s) in engineering-data.js: ${JSON.stringify(matches)}`,
    ).toEqual([]);
  });

  it("cv-data.js has no raw ms literals in source", () => {
    const src = readSource("../../../routes/public/cv/cv-data.js");
    const matches = src.match(RAW_MS_LITERAL) ?? [];
    expect(
      matches,
      `found raw ms literal(s) in cv-data.js: ${JSON.stringify(matches)}`,
    ).toEqual([]);
  });
});
