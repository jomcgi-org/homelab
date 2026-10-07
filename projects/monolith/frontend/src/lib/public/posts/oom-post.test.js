import { expect, test } from "vitest";
import manifest from "./posts-manifest.json";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";
import { figures } from "./ix/index.js";
import { splitInteractive } from "./ix/split.js";

const post = manifest.find((entry) => entry.slug === "125b-on-a-4090");
const { html } = renderDoc(post, buildPathIndex([]));
const parts = splitInteractive(html);
const slots = parts.filter((part) => part.ix);

test("every interactive figure is registered and keeps a no-JS fallback", () => {
  expect(slots.map((s) => s.ix)).toEqual([
    "fit",
    "routing",
    "residency",
    "context",
    "copy-queue",
    "draft-pays",
    "precision",
    "prefix-race",
    "concurrency",
    "published",
  ]);
  for (const slot of slots) {
    expect(figures[slot.ix]).toBeTypeOf("function");
    expect(slot.fallback).toMatch(/<(table|figure)\b/);
  }
});

test("the static figure keeps its drawing and key", () => {
  expect(html).toContain(
    "<figcaption>One layer: 10 of 512 routed experts run for a token</figcaption>",
  );
  expect(html.match(/<table class="fig-key">/g)).toHaveLength(1);
});
