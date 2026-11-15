import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HandoutText } from "./HandoutText";

const text = (markdown: string) =>
  render(<HandoutText markdown={markdown} />).container;

describe("HandoutText", () => {
  it("renders nothing for an empty handout", () => {
    expect(text("").innerHTML).toBe("");
    expect(render(<HandoutText markdown={null} />).container.innerHTML).toBe(
      "",
    );
  });

  it("renders headings below the handout's own title", () => {
    const root = text("# Hidden\n## Section\n### Detail");
    expect(root.querySelector("h1")).toBeNull();
    expect(root.textContent).not.toContain("Hidden");
    expect(root.querySelector("h4")?.textContent).toBe("Section");
    expect(root.querySelector("h5")?.textContent).toBe("Detail");
  });

  it("renders dash items inside a single list", () => {
    const root = text("- one\n- two");
    expect(root.querySelectorAll("ul")).toHaveLength(1);
    expect(
      [...root.querySelectorAll("ul > li")].map((item) => item.textContent),
    ).toEqual(["one", "two"]);
  });

  it("renders numbered items as an ordered list, separate from dashes", () => {
    const root = text("1. first\n2. second\n- loose");
    expect(
      [...root.querySelectorAll("ol > li")].map((item) => item.textContent),
    ).toEqual(["first", "second"]);
    expect(root.querySelector("ul > li")?.textContent).toBe("loose");
  });

  it("renders bold, italic and inline code", () => {
    const root = text("**bold** and *italic*, use `foo`");
    expect(root.querySelector("strong")?.textContent).toBe("bold");
    expect(root.querySelector("em")?.textContent).toBe("italic");
    expect(root.querySelector("code")?.textContent).toBe("foo");
    expect(root.querySelector("p")?.textContent).toBe(
      "bold and italic, use foo",
    );
  });

  it("nests bold inside italic and survives overlapping emphasis", () => {
    const nested = text("*a **b** c*");
    expect(nested.querySelector("em > strong")?.textContent).toBe("b");
    expect(nested.querySelector("em")?.textContent).toBe("a b c");
    expect(text("`a **b` c**").textContent).toBe("a b c");
  });

  it("joins consecutive quote lines into one blockquote", () => {
    const root = text("> a quote\n> goes on\n\nafter");
    expect(root.querySelectorAll("blockquote")).toHaveLength(1);
    expect(root.querySelector("blockquote")?.textContent).toBe(
      "a quote\ngoes on",
    );
    expect(root.querySelector("p")?.textContent).toBe("after");
  });

  it("keeps a fenced block verbatim", () => {
    const root = text("```\n+--+\n|**|\n+--+\n```\nafter");
    expect(root.querySelector("pre > code")?.textContent).toBe(
      "+--+\n|**|\n+--+",
    );
    expect(root.querySelector("pre strong")).toBeNull();
    expect(root.querySelector("p")?.textContent).toBe("after");
  });

  it("renders a pipe table with a header row", () => {
    const root = text("| Road | Days |\n|---|---|\n| North pass | *3* |");
    expect(
      [...root.querySelectorAll("th")].map((cell) => cell.textContent),
    ).toEqual(["Road", "Days"]);
    expect(
      [...root.querySelectorAll("td")].map((cell) => cell.textContent),
    ).toEqual(["North pass", "3"]);
    expect(root.querySelector("td em")?.textContent).toBe("3");
  });

  it("shows a wikilink as its display text, never as a link", () => {
    const root = text("see [[north-pass|the North Pass]] and [[Mira]]");
    expect(root.textContent).toBe("see the North Pass and Mira");
    expect(root.querySelector("a")).toBeNull();
  });

  it("keeps a piped wikilink inside one table cell", () => {
    const root = text(
      "| Place | Days |\n|---|---|\n| [[north-pass|the Pass]] (cold) | 3 |",
    );
    expect(root.querySelectorAll("td")).toHaveLength(2);
    expect(root.querySelector("td")?.textContent).toBe("the Pass (cold)");
  });

  describe("renders injected markup as inert text", () => {
    const inert = (markdown: string) => {
      const root = text(markdown);
      expect(
        root.querySelector(
          "script, img, iframe, object, embed, a, svg, style, link, form",
        ),
      ).toBeNull();
      for (const element of root.querySelectorAll("*"))
        for (const attribute of element.getAttributeNames())
          expect(attribute.startsWith("on") || attribute === "href").toBe(
            false,
          );
      return root;
    };

    it("shows a script tag as the characters typed", () => {
      const root = inert("<script>alert(1)</script>");
      expect(root.textContent).toBe("<script>alert(1)</script>");
    });

    it("shows an img onerror payload as text", () => {
      const payload = '<img src=x onerror="alert(1)">';
      expect(inert(payload).textContent).toBe(payload);
      expect(inert(`before ${payload} after`).textContent).toContain(payload);
    });

    it("shows iframe, object and embed as text", () => {
      for (const tag of ["iframe", "object", "embed"]) {
        const payload = `<${tag} src=//evil.example></${tag}>`;
        expect(inert(payload).textContent).toBe(payload);
      }
    });

    it("never turns a javascript: link into an anchor", () => {
      const payload = "[click](javascript:alert(1))";
      expect(inert(payload).textContent).toBe(payload);
    });

    it("never turns a data: URL into an anchor or a script", () => {
      const payload = "[x](data:text/html,<script>alert(1)</script>)";
      expect(inert(payload).textContent).toBe(payload);
    });

    it("keeps markup inert inside every block and inline form", () => {
      const payload = "<img src=x onerror=alert(1)>";
      const root = inert(
        [
          `## ${payload}`,
          `- **${payload}**`,
          `1. \`${payload}\``,
          `> *${payload}*`,
          "```",
          payload,
          "```",
          `| ${payload} | b |`,
          "|---|---|",
          `| [[${payload}|${payload}]] | c |`,
          '<span class="tag"><script>x()</script></span>',
        ].join("\n"),
      );
      expect(root.querySelector("span.tag")).toBeNull();
      expect(root.textContent).toContain(payload);
    });

    it("drops the private characters the parser uses for itself", () => {
      const root = inert("plain not bold x");
      expect(root.querySelector("strong, code")).toBeNull();
      expect(root.textContent).toBe("plain not bold x");
    });
  });
});
