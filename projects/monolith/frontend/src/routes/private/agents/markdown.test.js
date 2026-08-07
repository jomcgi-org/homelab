import { describe, it, expect } from "vitest";
import { renderAgentMarkdown } from "./markdown.js";

describe("renderAgentMarkdown", () => {
  it("renders https links as safe anchors", () => {
    const html = renderAgentMarkdown(
      "See [the listings](https://example.com/a?b=1&c=2) today",
    );
    expect(html).toContain(
      '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">the listings</a>',
    );
  });

  it("renders links inside list items", () => {
    const html = renderAgentMarkdown("- [x](https://example.com)");
    expect(html).toMatch(/<li>.*<a href="https:\/\/example\.com".*<\/li>/);
  });

  it("leaves javascript: and data: links as inert text", () => {
    for (const bad of [
      "[click](javascript:alert(1))",
      "[x](data:text/html,<script>alert(1)</script>)",
    ]) {
      const html = renderAgentMarkdown(bad);
      expect(html).not.toContain("href");
      expect(html).not.toMatch(/<a[\s>]/i);
    }
  });

  it("escapes HTML in link labels", () => {
    const html = renderAgentMarkdown("[<img onerror=x>](https://example.com)");
    expect(html).not.toMatch(/<img/i);
    expect(html).toContain("&lt;img");
  });

  it("cannot be forged with an injected sentinel", () => {
    const html = renderAgentMarkdown("\x00AGENTLINK0\x00 and plain text");
    expect(html).not.toContain("<a ");
    expect(html).toContain("plain text");
  });

  it("still escapes injected HTML", () => {
    const html = renderAgentMarkdown("<script>alert(1)</script>");
    expect(html).not.toMatch(/<script>/i);
    expect(html).toContain("&lt;script&gt;");
  });

  it("strips voice summaries", () => {
    const html = renderAgentMarkdown(
      "The answer.\n\n<voice>Spoken duplicate.</voice>",
    );
    expect(html).toContain("The answer.");
    expect(html).not.toContain("Spoken duplicate");
    expect(html).not.toContain("voice");
  });

  it("keeps a top-level heading by downgrading it to h2", () => {
    expect(renderAgentMarkdown("# Plan")).toContain("<h2>Plan</h2>");
  });

  it("renders bold, code, and fences from agent output", () => {
    const html = renderAgentMarkdown(
      "**Saturday:** run `ci`\n\n```\nplain block\n```",
    );
    expect(html).toContain("<strong>Saturday:</strong>");
    expect(html).toContain("<code>ci</code>");
    expect(html).toContain("<pre><code>plain block</code></pre>");
  });
});
