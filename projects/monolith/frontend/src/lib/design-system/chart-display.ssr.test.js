import { expect, it, vi } from "vitest";
import { serverRender } from "../../../test/data-display/server-render.js";

it("renders ChartFrame and Legend without browser globals", async () => {
  for (const name of ["window", "document", "navigator"])
    vi.stubGlobal(name, undefined);
  try {
    const { body } = await serverRender("ChartHarness");
    expect(body).toContain("<figure");
    expect(body).toContain("<figcaption");
    expect(body).toContain("<table");
    expect(body).toContain('scope="row"');
    for (const name of [
      "gpu",
      "host-ram",
      "page-cache",
      "nvme",
      "hot-expert-set",
    ])
      expect(body).toContain(`data-series-role="${name}"`);
    for (const name of ["window", "document", "navigator"])
      expect(globalThis[name]).toBeUndefined();
  } finally {
    vi.unstubAllGlobals();
  }
});

it.each(
  ["title", "units", "description"].flatMap((prop) =>
    ["ready", "loading", "empty", "error", "unavailable"].flatMap((state) =>
      [undefined, null, "", " \n\t"].map((value) => ({ prop, state, value })),
    ),
  ),
)(
  "rejects missing or blank chart $prop ($state, $value) loudly during render",
  async ({ prop, state, value }) => {
    await expect(
      serverRender("ChartHarness", { [prop]: value, state }),
    ).rejects.toThrow(`chart ${prop} must be non-empty text`);
  },
);

it("requires a fallback snippet even when chart content is unavailable", async () => {
  for (const state of ["ready", "unavailable"]) {
    await expect(
      serverRender("ChartFrame", {
        title: "Chart",
        units: "bytes",
        description: "Synthetic",
        state,
      }),
    ).rejects.toThrow("chart fallback snippet is required");
  }
});

it.each([
  [{ entries: [{ id: "other", label: "Other" }] }, /Unknown series role/],
  [
    {
      entries: [
        { id: "gpu", label: "GPU" },
        { id: "gpu", label: "Again" },
      ],
    },
    /Duplicate series role/,
  ],
  [{ entries: [{ id: "gpu", label: " " }] }, /series label/],
  [{ entries: null }, /must be an array/],
  [{ label: " " }, /legend label/],
])("rejects invalid legend inputs %j", async (props, error) => {
  await expect(serverRender("Legend", props)).rejects.toThrow(error);
});

it("rejects unknown chart content state", async () => {
  await expect(
    serverRender("ChartHarness", { state: "success" }),
  ).rejects.toThrow(/content state/);
});
