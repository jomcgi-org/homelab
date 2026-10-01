import { describe, expect, it } from "vitest";
import { markdownBlocks, plainPreview, safeHref } from "./markdown.js";

describe("markdownBlocks", () => {
  it("renders a reply's paragraphs, emphasis and code spans as runs", () => {
    const blocks = markdownBlocks(
      "PR #6536 now wires TLC in, behind `conformance.s6.enabled`.\n\n**What changed**",
    );
    expect(blocks).toEqual([
      {
        type: "paragraph",
        inline: [
          { text: "PR #6536 now wires TLC in, behind " },
          { code: "conformance.s6.enabled" },
          { text: "." },
        ],
      },
      {
        type: "paragraph",
        inline: [{ strong: [{ text: "What changed" }] }],
      },
    ]);
  });

  it("keeps headings, lists, nested lists and tight item text", () => {
    const blocks = markdownBlocks(
      "## Scope\n- **Export.** Serialise it.\n  - `Succeed` is two records\n- Second",
    );
    expect(blocks[0]).toEqual({
      type: "heading",
      depth: 2,
      inline: [{ text: "Scope" }],
    });
    const list = blocks[1];
    expect(list.type).toBe("list");
    expect(list.ordered).toBe(false);
    expect(list.items).toHaveLength(2);
    expect(list.items[0].blocks[0]).toEqual({
      type: "text",
      inline: [{ strong: [{ text: "Export." }] }, { text: " Serialise it." }],
    });
    expect(list.items[0].blocks[1].type).toBe("list");
    expect(list.items[0].blocks[1].items[0].blocks[0].inline[0]).toEqual({
      code: "Succeed",
    });
  });

  it("keeps a task list's boxes inline ahead of their text", () => {
    const [list] = markdownBlocks("- [ ] open\n- [x] done");
    expect(list.items.map((item) => [item.task, item.checked])).toEqual([
      [true, false],
      [true, true],
    ]);
    expect(list.items[0].blocks[0].inline).toEqual([
      { box: false },
      { text: "open" },
    ]);
  });

  it("numbers an ordered list from its start", () => {
    const [list] = markdownBlocks("3. three\n4. four");
    expect(list.ordered).toBe(true);
    expect(list.start).toBe(3);
  });

  it("carries fenced code with its language and a table with its cells", () => {
    const blocks = markdownBlocks(
      "```py\nprint(1)\n```\n\n| Window | Peak |\n|---|---|\n| 10 | 76 MB |",
    );
    expect(blocks[0]).toEqual({ type: "code", lang: "py", text: "print(1)" });
    expect(blocks[1].type).toBe("table");
    expect(blocks[1].header).toEqual([
      [{ text: "Window" }],
      [{ text: "Peak" }],
    ]);
    expect(blocks[1].rows).toEqual([[[{ text: "10" }], [{ text: "76 MB" }]]]);
  });

  it("keeps a blockquote and a rule", () => {
    const blocks = markdownBlocks("> quoted\n\n---");
    expect(blocks[0].type).toBe("quote");
    expect(blocks[0].blocks[0].inline).toEqual([{ text: "quoted" }]);
    expect(blocks[1]).toEqual({ type: "rule" });
  });

  it("turns raw HTML into visible text, inline and block alike", () => {
    const blocks = markdownBlocks(
      "hi <b>x</b> <script>alert(1)</script>\n\n<div>block</div>",
    );
    expect(blocks[0].inline.map((run) => run.text).join("")).toBe(
      "hi <b>x</b> <script>alert(1)</script>",
    );
    expect(blocks[1]).toEqual({
      type: "paragraph",
      inline: [{ text: "<div>block</div>" }],
    });
  });

  it("keeps only links a reader can follow and drops images to their alt", () => {
    const [{ inline }] = markdownBlocks(
      "[bad](javascript:alert(1)) [ok](https://x.y/z) ![shot](http://a/b.png)",
    );
    expect(inline[0]).toEqual({ link: null, inline: [{ text: "bad" }] });
    expect(inline[2]).toEqual({
      link: "https://x.y/z",
      inline: [{ text: "ok" }],
    });
    expect(inline[4]).toEqual({ text: "shot" });
  });

  it("returns nothing for empty or non-string input", () => {
    expect(markdownBlocks("")).toEqual([]);
    expect(markdownBlocks("   \n")).toEqual([]);
    expect(markdownBlocks(null)).toEqual([]);
    expect(markdownBlocks(42)).toEqual([]);
  });
});

describe("safeHref", () => {
  it("accepts http, https, mailto, relative and fragment links", () => {
    for (const href of [
      "https://a.b",
      "http://a.b",
      "mailto:x@y.z",
      "/slop",
      "#step-2",
      "./x",
      "../x",
    ]) {
      expect(safeHref(href)).toBe(href);
    }
  });

  it("rejects every other scheme", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,x")).toBeNull();
    expect(safeHref("")).toBeNull();
    expect(safeHref(null)).toBeNull();
  });
});

describe("plainPreview", () => {
  it("drops markdown marks and flattens to one line", () => {
    expect(
      plainPreview(
        "I wrote the plan to `a/b.json`.\n\n**What's done:** [x](https://y)",
      ),
    ).toBe("I wrote the plan to a/b.json. What's done: x");
  });

  it("clips to the width on one ellipsis", () => {
    const text = plainPreview("word ".repeat(100), 40);
    expect(text).toHaveLength(40);
    expect(text.endsWith("…")).toBe(true);
  });

  it("skips fenced code and tolerates no text", () => {
    expect(plainPreview("```\nx\n```\nafter")).toBe("after");
    expect(plainPreview(null)).toBe("");
  });
});
