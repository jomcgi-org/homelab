import { expect, it, vi } from "vitest";
import { serverRender } from "../../../test/data-display/server-render.js";

it("SSR imports and renders every synthetic state without browser globals", async () => {
  for (const name of ["window", "document", "navigator"])
    vi.stubGlobal(name, undefined);
  try {
    const { body } = await serverRender();
    expect(body.replace(/<!--[\s\S]*?-->/g, "")).toMatch(
      /<h3[^>]*>Synthetic measurements<\/h3>/,
    );
    expect(body).toContain("999,950 bytes");
    expect(body).toContain("-12,345 bytes");
    expect(body).toContain("0.125 seconds");
    expect(body).toContain("0 requests");
    expect(body).toContain('data-ds-theme="technical-drawing-dark"');
    expect(body).toContain('data-density="dense"');
    expect(body).toContain('data-density="sparse"');
    expect(body).toContain("LongSyntheticUnitWithoutBreaksForWrapping");
    for (const state of ["Loading", "Empty", "Error", "Unavailable"])
      expect(body).toContain(`${state}: Synthetic state explanation`);
    expect(body).not.toContain("Hidden stale content");
    expect(body).not.toMatch(/aria-live|role="status"/);
    for (const name of ["window", "document", "navigator"])
      expect(globalThis[name]).toBeUndefined();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("SSR uses the same explicit locale for visible and exact values", async () => {
  const { body } = await serverRender("Metric", {
    label: "Bytes",
    value: 12345.6789,
    unit: "bytes",
    locale: "de-DE",
  });
  expect(body).toContain("12.345,6789 bytes");
  expect(body).not.toContain("12,345.6789");
});

it.each([2, 3, 4, 5, 6])(
  "SSR renders a real h%s and labelled section",
  async (headingLevel) => {
    const { body } = await serverRender("Panel", {
      title: "Title",
      headingLevel,
    });
    const id = body.match(/aria-labelledby="([^"]+)"/)[1];
    expect(body).toContain(`<h${headingLevel} id="${id}"`);
  },
);

it.each([1, 7, 2.5, "3", null])(
  "SSR rejects invalid heading level %s",
  async (headingLevel) => {
    await expect(
      serverRender("Panel", { title: "Title", headingLevel }),
    ).rejects.toThrow(/headingLevel/);
  },
);

it.each([
  ["Panel", { title: " " }, /title/],
  ["Status", { label: " " }, /status label/],
  ["Status", { label: "Bad", kind: "success" }, /status kind/],
  ["Metric", { label: "" }, /metric label/],
  ["Metric", { label: "Valid", state: "success" }, /content state/],
  ["KeyValue", { rows: [{ label: "", value: 0 }] }, /row label/],
  ["KeyValue", { density: "compact" }, /density/],
])("SSR validates %s presentation inputs", async (component, props, error) => {
  await expect(serverRender(component, props)).rejects.toThrow(error);
});
