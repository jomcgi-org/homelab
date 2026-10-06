// @vitest-environment happy-dom
import { afterEach, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";
import { Legend, SERIES_ROLES } from "@homelab/design-system/data-display";
import ChartHarness from "../../../test/data-display/ChartHarness.svelte";

const mounted = [];
afterEach(async () => {
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});
async function render(component, props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  mounted.push({ component: mount(component, { target, props }), target });
  await tick();
  return target;
}

it("links figure title and description and exposes a captioned table by keyboard disclosure", async () => {
  const target = await render(ChartHarness);
  const figure = target.querySelector("figure");
  expect(
    target.querySelector(`#${figure.getAttribute("aria-labelledby")}`)
      .textContent,
  ).toBe("Synthetic chart");
  expect(
    target.querySelector(`#${figure.getAttribute("aria-describedby")}`)
      .textContent,
  ).toBe("Exact synthetic measurements");
  expect(figure.querySelector("figcaption .units").textContent).toBe(
    "Units: bytes",
  );
  const summary = figure.querySelector("summary");
  expect(summary.textContent).toBe("Read chart data");
  summary.focus();
  expect(document.activeElement).toBe(summary);
  summary.click();
  expect(figure.querySelector("details").open).toBe(true);
  const table = figure.querySelector("details table");
  expect(table.querySelector("caption").textContent).toBe(
    "Synthetic measurements: ready",
  );
  expect(
    [...table.querySelectorAll('thead th[scope="col"]')].map(
      (node) => node.textContent,
    ),
  ).toEqual(["Series", "Bytes"]);
  expect(table.querySelector('tbody th[scope="row"]').textContent).toBe("GPU");
  expect(table.querySelector("tbody td").textContent).toBe("0");
  expect(target.querySelector('[aria-live], [role="status"]')).toBeNull();
});

it.each(["light", "dark"])(
  "retains contract order, role colours, text labels and distinct SVG shapes in %s",
  async (scheme) => {
    const entries = [...SERIES_ROLES]
      .reverse()
      .map((entry) => ({ id: entry.id, label: `Synthetic ${entry.label}` }));
    const target = await render(Legend, { entries });
    target.dataset.dsTheme = `technical-drawing-${scheme}`;
    const items = [...target.querySelectorAll("li")];
    expect(items.map((node) => node.dataset.seriesRole)).toEqual(
      SERIES_ROLES.map((entry) => entry.id),
    );
    expect(items.map((node) => node.dataset.marker)).toEqual([
      "circle",
      "square",
      "triangle",
      "diamond",
      "cross",
    ]);
    expect(items.map((node) => node.textContent.trim())).toEqual(
      SERIES_ROLES.map((entry) => `Synthetic ${entry.label} (${entry.marker})`),
    );
    expect(
      new Set(items.map((node) => node.querySelector("svg").innerHTML)).size,
    ).toBe(5);
    for (const [index, node] of items.entries()) {
      expect(node.querySelector("svg").style.getPropertyValue("fill")).toBe(
        `var(${SERIES_ROLES[index].role}, var(--ds-ink))`,
      );
      expect(node.querySelector("svg").getAttribute("aria-hidden")).toBe(
        "true",
      );
    }
    expect(entries.map((entry) => entry.id)).toEqual(
      [...SERIES_ROLES].reverse().map((entry) => entry.id),
    );
  },
);

it.each(["loading", "empty", "error", "unavailable"])(
  "renders %s text and usable fallback, not stale chart content",
  async (state) => {
    const target = await render(ChartHarness, { state });
    expect(target.querySelector("figure").dataset.state).toBe(state);
    expect(target.querySelector(".state").textContent.toLowerCase()).toBe(
      `${state}: synthetic explanation`,
    );
    expect(target.querySelector(".chart, ul")).toBeNull();
    expect(target.querySelector("table caption").textContent).toBe(
      `Synthetic measurements: ${state}`,
    );
    expect(target.querySelector("figure").getAttribute("aria-busy")).toBe(
      state === "loading" ? "true" : null,
    );
  },
);
