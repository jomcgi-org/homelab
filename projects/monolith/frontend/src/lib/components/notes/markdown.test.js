import { describe, it, expect } from "vitest";
import { renderMarkdown } from "./markdown.js";

describe("renderMarkdown", () => {
  const titleMap = new Map([["Existing Note", { id: "id-existing" }]]);

  it("renders headings", () => {
    expect(renderMarkdown("## hello", titleMap)).toContain("<h2>hello</h2>");
  });

  it("renders dash list items inside a single <ul>", () => {
    const html = renderMarkdown("- one\n- two", titleMap);
    expect(html).toMatch(/<ul>\s*<li>one<\/li>\s*<li>two<\/li>\s*<\/ul>/);
  });

  it("renders bold and italic", () => {
    const html = renderMarkdown("**bold** and *italic*", titleMap);
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<em>italic</em>");
  });

  it("renders inline code", () => {
    expect(renderMarkdown("use `foo`", titleMap)).toContain("<code>foo</code>");
  });

  it("renders blockquotes", () => {
    expect(renderMarkdown("> a quote", titleMap)).toContain("<blockquote>");
  });

  it("resolves wikilinks to live anchors", () => {
    const html = renderMarkdown("see [[Existing Note]]", titleMap);
    expect(html).toContain('class="wl"');
    expect(html).toContain('data-id="id-existing"');
  });

  it("renders unresolved wikilinks as dead links", () => {
    const html = renderMarkdown("see [[Missing]]", titleMap);
    expect(html).toContain('class="wl dead"');
    expect(html).not.toContain("data-id=");
  });

  it("escapes HTML in source", () => {
    expect(renderMarkdown("<script>", titleMap)).toContain("&lt;script&gt;");
  });

  it("preserves rendered tag spans through the escape pass", () => {
    const html = renderMarkdown(
      'foo <span class="tag">#x</span> bar',
      titleMap,
    );
    expect(html).toContain('<span class="tag">#x</span>');
    expect(html).not.toContain("&lt;span");
  });

  // Obsidian piped wikilinks: [[target|display]] should resolve to the
  // target's id while showing display as the link text. Pre-fix the
  // renderer looked up the literal "target|display" string in titleMap
  // (always missed) and rendered the pipe through to the visible link.
  it("resolves piped wikilinks via the target slug", () => {
    const map = new Map([["book-on-writing-well", { id: "id-zinsser" }]]);
    const html = renderMarkdown(
      "see [[book-on-writing-well|On Writing Well]]",
      map,
    );
    expect(html).toContain('class="wl"');
    expect(html).toContain('data-id="id-zinsser"');
    expect(html).toContain(">On Writing Well</a>");
  });

  // Pipes inside [[…|…]] must not be treated as table-cell separators.
  // Pre-fix `splitRow` naively split on `|`, so a single piped wikilink
  // inside a row split it into too many cells and visually broke the
  // link across columns.
  it("preserves piped wikilinks inside table cells", () => {
    const map = new Map([["book-on-writing-well", { id: "id-zinsser" }]]);
    const md = [
      "| Book | Shared Ground |",
      "|---|---|",
      "| *[[book-on-writing-well|On Writing Well]]* (Zinsser) | Cut clutter |",
    ].join("\n");
    const html = renderMarkdown(md, map);
    // Exactly two body cells, not three: the wikilink's pipe should
    // not have leaked into a column boundary.
    const tdCount = (html.match(/<td>/g) || []).length;
    expect(tdCount).toBe(2);
    // The wikilink resolves to its target and shows the display text.
    expect(html).toContain('data-id="id-zinsser"');
    expect(html).toContain(">On Writing Well</a>");
    // First cell holds the link AND the trailing "(Zinsser)" — wasn't
    // split mid-link.
    expect(html).toMatch(
      /<td><em>.*On Writing Well.*<\/em>\s*\(Zinsser\)<\/td>/,
    );
  });
});
