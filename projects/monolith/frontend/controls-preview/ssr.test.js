import { expect, it, vi } from "vitest";
import { renderFixture } from "./server-render.js";

it("imports all package components and renders native semantics without browser globals", async () => {
  for (const name of ["window", "document", "localStorage", "matchMedia"])
    vi.stubGlobal(name, undefined);
  try {
    const { body } = await renderFixture();
    for (const name of ["window", "document", "localStorage", "matchMedia"])
      expect(globalThis[name]).toBeUndefined();
    for (const html of [
      "<header",
      "<h2",
      '<nav aria-label="Breadcrumb"',
      "<ol",
      'aria-current="page"',
      "<label",
      "<select",
      "(required)",
      "Error: Choose another region",
      "<details",
      "<summary",
      'role="tablist"',
      'role="tab"',
      'role="tabpanel"',
      'aria-selected="true"',
      'aria-label="Named action"',
      'data-sample="nested"',
    ])
      expect(body).toContain(html);
    expect(
      body.match(/<button[^>]* type="(?:button|submit|reset)"/g)?.length,
    ).toBe(body.match(/<button\b/g)?.length);
  } finally {
    vi.unstubAllGlobals();
  }
});
