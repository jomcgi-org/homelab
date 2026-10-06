import { expect, it, vi } from "vitest";
import { FIXTURES, SECTIONS } from "./fixtures.js";
import { renderFixture } from "./server-render.js";

it("renders both compositions and all focused states without browser globals", async () => {
  const globals = [
    "window",
    "document",
    "navigator",
    "localStorage",
    "matchMedia",
  ];
  for (const name of globals) vi.stubGlobal(name, undefined);
  try {
    const { body } = await renderFixture();
    for (const name of globals) expect(globalThis[name]).toBeUndefined();
    for (const section of SECTIONS)
      expect(
        body.match(new RegExp(`data-gallery-section="${section}"`, "g")),
      ).toHaveLength(2);
    for (const boundary of ["unmarked", "light", "dark", "nested", "sibling"])
      expect(body).toContain(`data-gallery-boundary="${boundary}"`);
    expect(body.match(/data-ds-theme="technical-drawing-light"/g)).toHaveLength(
      1,
    );
    expect(body.match(/data-ds-theme="technical-drawing-dark"/g)).toHaveLength(
      2,
    );
    for (const state of FIXTURES.states) {
      expect(body).toContain(`data-gallery-content-state="${state}"`);
      expect(body).toContain(`data-gallery-chart-state="${state}"`);
    }
    for (const html of [
      "<header",
      '<nav aria-label="Breadcrumb"',
      'aria-current="page"',
      'role="tablist"',
      'aria-selected="true"',
      "<label",
      "(required)",
      'aria-invalid="true"',
      "<input",
      " disabled",
      "<details",
      "<summary",
      "<caption",
      'scope="row"',
      'data-gallery-series="gpu"',
      "999,950 bytes",
      "0 requests",
      "Error: Reference must contain a sheet label",
      'data-state="unavailable"',
    ])
      expect(body).toContain(html);
    expect(body.match(/data-gallery-fallback/g)).toHaveLength(4);
  } finally {
    vi.unstubAllGlobals();
  }
});
