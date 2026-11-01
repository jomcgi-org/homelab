import { expect, test } from "vitest";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";
import { splitFigures } from "./split.js";

const render = (content) =>
  renderDoc({ path: "docs/posts/x.md", content }, buildPathIndex([])).html;

test("a marker takes the next N blocks as its markdown version", () => {
  const html = render(
    [
      "Before.",
      "",
      "```figure",
      "demo 2",
      "```",
      "",
      "| A | B |",
      "|---|---|",
      "| 1 | 2 |",
      "",
      "> Quoted.",
      "",
      "After.",
    ].join("\n"),
  );
  const parts = splitFigures(html);
  expect(parts.map((p) => p.ix ?? "html")).toEqual(["html", "demo", "html"]);
  expect(parts[1].fallback).toContain("<table>");
  expect(parts[1].fallback).toContain("<blockquote>");
  expect(parts[2].html).toContain("After.");
  expect(parts[2].html).not.toContain("Quoted.");
});

test("nested elements of the same tag stay inside one block", () => {
  const html =
    '<div class="ix-slot" data-ix="x" data-ix-blocks="1"></div>\n<div><div>in</div></div><p>out</p>';
  const [part, rest] = splitFigures(html);
  expect(part.fallback).toBe("<div><div>in</div></div>");
  expect(rest.html).toBe("<p>out</p>");
});

test("an invalid marker stays an ordinary code block", () => {
  const html = render("```figure\nNot A Name\n```\n");
  expect(html).toContain("doc-code");
  expect(splitFigures(html)).toEqual([{ html }]);
});

test("a zero count keeps the next block on the page", () => {
  const html = render("```figure\ndemo 0\n```\n\n| A |\n|---|\n| 1 |\n");
  const [figure, rest] = splitFigures(html);
  expect(figure).toEqual({ ix: "demo", fallback: "" });
  expect(rest.html).toContain("<table>");
});
