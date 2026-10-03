import { expect, test } from "vitest";
import manifest from "./posts-manifest.json";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";
import { splitSystemDiagrams } from "./system-diagrams.js";

test("all four authored figures become systems views with their original explanations", () => {
  const post = manifest.find((entry) => entry.slug === "125b-on-a-4090");
  const { html } = renderDoc(post, buildPathIndex([]));
  const parts = splitSystemDiagrams(html);
  const diagrams = parts.filter((part) => part.diagram);
  expect(diagrams.map((part) => part.diagram)).toEqual([
    "memory",
    "prefill",
    "decode",
    "swap",
  ]);
  expect(diagrams.map((part) => part.notes.length)).toEqual([6, 7, 9, 5]);
  for (const part of diagrams) {
    for (const note of part.notes) expect(html).toContain(note.html);
  }
  expect(
    parts
      .filter((part) => part.html)
      .map((part) => part.html)
      .join(""),
  ).toContain("can&#39;t convince myself to upgrade in this RAM economy");
});

test("unknown figures and recognised figures without keys retain the complete fallback", () => {
  for (const title of ["Another diagram", "Where are the weights?"]) {
    const html = `<p>Before</p><figure class="fig"><figcaption>${title}</figcaption><svg></svg></figure><table class="fig-key"><tbody></tbody></table><p>After</p>`;
    expect(splitSystemDiagrams(html)).toEqual([{ html }]);
  }
});
