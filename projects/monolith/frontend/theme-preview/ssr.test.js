import { expect, it, vi } from "vitest";

it("loads and server-renders the fixture and CSS export without browser globals", async () => {
  for (const name of [
    "window",
    "document",
    "navigator",
    "localStorage",
    "matchMedia",
  ])
    vi.stubGlobal(name, undefined);
  try {
    const { renderFixture } = await import("./server-render.js");
    const { body } = await renderFixture();
    expect(body.match(/data-ds-theme="technical-drawing-light"/g)).toHaveLength(
      1,
    );
    expect(body.match(/data-ds-theme="technical-drawing-dark"/g)).toHaveLength(
      2,
    );
    expect(body).toContain('data-sample="nested"');
    expect(body).toContain("Dark inset in light");
    expect(body.match(/data-data-display/g)).toHaveLength(3);
    for (const text of [
      "Synthetic data display",
      "999,950 bytes",
      "0 requests",
      "0.125 seconds",
      "Synthetic memory tiers",
      "LongSyntheticUnitWithoutBreaksForWrapping",
    ])
      expect(body).toContain(text);
    for (const state of ["loading", "empty", "error", "unavailable"])
      expect(body).toContain(`data-state-case="${state}"`);
    expect(body).not.toContain("Hidden stale chart");
    for (const name of [
      "window",
      "document",
      "navigator",
      "localStorage",
      "matchMedia",
    ])
      expect(globalThis[name]).toBeUndefined();
    await expect(
      import("@homelab/design-system/tokens/technical-drawing.css"),
    ).resolves.toBeDefined();
  } finally {
    vi.unstubAllGlobals();
  }
});
