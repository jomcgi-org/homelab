// The post's fallback tables (what readers see without JavaScript) must say
// what the interactive figures say. Each check derives the table's numbers
// from the figure's data module and looks for them in the rendered fallback.
import { expect, test } from "vitest";
import manifest from "../posts-manifest.json";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";
import { splitInteractive } from "./split.js";
import { expertPaths } from "./data.js";
import { weights } from "./data-fit.js";
import * as context from "./data-context.js";
import { results } from "./data-copy-queue.js";
import { draftRate } from "./data-draft-pays.js";
import { settings } from "./data-precision.js";
import { runs } from "./data-prefix-race.js";
import { rows } from "./data-concurrency.js";
import { published } from "./data-published.js";

const post = manifest.find((entry) => entry.slug === "125b-on-a-4090");
const fallback = Object.fromEntries(
  splitInteractive(renderDoc(post, buildPathIndex([])).html)
    .filter((part) => part.ix)
    .map((part) => [part.ix, part.fallback]),
);

test("fit", () => {
  for (const w of weights)
    expect(fallback.fit).toContain(`${w.gb.toFixed(2)} GB`);
});

test("residency", () => {
  for (const p of expertPaths.filter((p) => p.micros && !p.illustrative))
    expect(fallback.residency).toContain(`About ${p.micros} µs`);
});

test("context", () => {
  for (const tokens of [2_048, 32_768, 131_072, 262_144]) {
    const { bytes, records } = context.kvCache(tokens);
    expect(fallback.context).toContain(`${context.gb(bytes)} GB`);
    expect(fallback.context).toContain(records.toLocaleString("en-US"));
  }
});

test("copy-queue", () => {
  for (const r of results) {
    expect(fallback["copy-queue"]).toContain(
      `${r.before.toFixed(r.before < 20 ? 2 : 1)} s`,
    );
    expect(fallback["copy-queue"]).toContain(
      `${r.after.toFixed(r.after < 20 ? 2 : 1)} s`,
    );
  }
});

test("draft-pays", () => {
  for (const kept of [3, 4, 5, 7]) {
    const { low, high } = draftRate(kept);
    expect(fallback["draft-pays"]).toContain(
      `${Math.round(low)}–${Math.round(high)} tok/s`,
    );
  }
});

test("precision", () => {
  for (const s of settings.filter((s) => s.kl)) {
    const [lo, hi] = s.kl;
    expect(fallback.precision).toContain(
      lo === hi ? `KL ${lo}` : `KL ${lo.toFixed(2)}–${hi.toFixed(2)}`,
    );
  }
});

test("prefix-race", () => {
  for (const r of runs)
    expect(fallback["prefix-race"]).toContain(`${r.seconds} s`);
});

test("concurrency", () => {
  for (const r of rows) {
    expect(fallback.concurrency).toContain(r.batched.aggregate.toFixed(1));
    expect(fallback.concurrency).toContain(r.batched.perStream.toFixed(1));
    expect(fallback.concurrency).toContain(r.serial.aggregate.toFixed(1));
  }
});

test("published", () => {
  for (const p of published)
    expect(fallback.published).toContain(p.from.toFixed(1));
});
