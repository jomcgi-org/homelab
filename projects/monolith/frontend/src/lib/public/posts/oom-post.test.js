import { expect, test } from "vitest";
import manifest from "./posts-manifest.json";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";

const post = manifest.find((entry) => entry.slug === "125b-on-a-4090");
const { html } = renderDoc(post, buildPathIndex([]));

test("the engine post renders its four keyed static figures", () => {
  for (const title of [
    "One layer: 10 of 512 routed experts run for a token",
    "A GPU-resident baseline against the three expert tiers",
    "Expert records and PLE rows take different paths",
    "Context state grows with attention layers and shrinks the expert cache",
  ]) {
    expect(html).toContain(`<figcaption>${title}</figcaption>`);
  }
  expect(html.match(/<table class="fig-key">/g)).toHaveLength(4);
  expect(html.match(/<svg/g)).toHaveLength(4);
});

test("recording details fold away and the caveat appears once", () => {
  expect(html).toContain("<details><summary>Recording details</summary>");
  expect(html.match(/What these numbers don/g)).toHaveLength(1);
});
