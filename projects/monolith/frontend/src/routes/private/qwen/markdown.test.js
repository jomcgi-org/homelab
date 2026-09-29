import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown.js";

describe("Qwen markdown", () => {
  it.each([
    "[click](javascript:alert%281%29)",
    "[click](JaVaScRiPt:alert%281%29)",
    "[click](data:text/html;base64,PHNjcmlwdD4=)",
    "[click](vbscript:msgbox%281%29)",
    "[click](<java\tscript:alert(1)>)",
  ])("removes executable link destinations: %s", (input) => {
    expect(renderMarkdown(input)).not.toContain("<a ");
    expect(renderMarkdown(input)).toContain("click");
  });
  it("keeps entity-obfuscated schemes as literal relative URLs", () => {
    expect(renderMarkdown("[click](javascript&#58;alert%281%29)")).toContain(
      'href="javascript&amp;#58;alert%281%29"',
    );
  });
  it("escapes raw HTML and attribute injection", () => {
    expect(renderMarkdown('<img src=x onerror="alert(1)">')).not.toContain(
      "<img",
    );
    const html = renderMarkdown(
      '[click](<https://example.com/"onmouseover="alert(1)>)',
    );
    expect(html).not.toContain('"onmouseover="');
    expect(html).toContain("&quot;");
  });
  it("preserves formatted labels and safe destinations", () => {
    expect(renderMarkdown("[**site**](https://example.com)")).toContain(
      '<a href="https://example.com"><strong>site</strong></a>',
    );
    expect(renderMarkdown("[email](mailto:joe@example.com)")).toContain(
      'href="mailto:joe@example.com"',
    );
    expect(renderMarkdown("[local](/qwen)")).toContain('href="/qwen"');
  });
  it("rejects data images and escapes image attributes", () => {
    expect(renderMarkdown("![image](data:image/svg+xml,test)")).not.toContain(
      "<img",
    );
    expect(
      renderMarkdown('![image](https://example.com/image.png "a & b")'),
    ).toContain('title="a &amp; b"');
  });
});
