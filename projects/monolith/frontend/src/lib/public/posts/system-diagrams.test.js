import { expect, test } from "vitest";
import manifest from "./posts-manifest.json";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";
import { splitSystemDiagrams } from "./system-diagrams.js";

test("legacy figures become systems views while preserving their key explanations", () => {
  const fixtures = [
    ["Where are the weights?", "memory", ["1", "2", "3", "4", "5", "6"]],
    ["Prefill chunk decisions", "prefill", ["1", "2", "3", "A", "C", "D", "E"]],
    [
      "One decode step through one expert layer",
      "decode",
      ["1", "2", "3", "4", "A", "B", "C", "D", "E"],
    ],
    [
      "How one hot-set slot changes hands without a stall",
      "swap",
      ["1", "2", "3", "A", "B"],
    ],
  ];
  const html =
    "<p>Before the figures.</p>" +
    fixtures
      .map(
        ([title, mode, keys]) =>
          `<figure class="fig"><figcaption>${title}</figcaption><svg></svg></figure>` +
          `<table class="fig-key"><tbody>${keys
            .map(
              (key) =>
                `<tr><td><span class="co">${key}</span></td><td>Explanation for <em>${mode} ${key}</em>.</td></tr>`,
            )
            .join("")}</tbody></table>`,
      )
      .join("") +
    "<p>After the figures.</p>";
  const parts = splitSystemDiagrams(html);
  const diagrams = parts.filter((part) => part.diagram);
  expect(diagrams.map((part) => part.diagram)).toEqual(
    fixtures.map(([, mode]) => mode),
  );
  for (const [index, part] of diagrams.entries()) {
    const [title, mode, keys] = fixtures[index];
    expect(part.title).toBe(title);
    expect(part.notes).toEqual(
      keys.map((key) => ({
        key,
        html: `Explanation for <em>${mode} ${key}</em>.`,
      })),
    );
  }
  expect(
    parts
      .filter((part) => part.html)
      .map((part) => part.html)
      .join(""),
  ).toBe("<p>Before the figures.</p><p>After the figures.</p>");
});

test("the current engine figure keeps its static drawing and key table", () => {
  const post = manifest.find((entry) => entry.slug === "125b-on-a-4090");
  const { html } = renderDoc(post, buildPathIndex([]));
  expect(html).toContain(
    "<figcaption>Expert records and PLE rows take different paths</figcaption>",
  );
  expect(html).toContain('<table class="fig-key">');
  expect(html).toContain("<svg");
  expect(splitSystemDiagrams(html)).toEqual([{ html }]);
});

test("unknown figures and recognised figures without keys retain the complete fallback", () => {
  for (const title of ["Another diagram", "Where are the weights?"]) {
    const html = `<p>Before</p><figure class="fig"><figcaption>${title}</figcaption><svg></svg></figure><table class="fig-key"><tbody></tbody></table><p>After</p>`;
    expect(splitSystemDiagrams(html)).toEqual([{ html }]);
  }
});
