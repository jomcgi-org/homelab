// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { mount, hydrate, tick, unmount } from "svelte";
import {
  Panel,
  Section,
  KeyValue,
  Status,
  Metric,
  STATUS_KINDS,
  DATA_DISPLAY_FIXTURES,
  formatMeasurement,
} from "@homelab/design-system/data-display";
import Fixture from "../../../test/data-display/Fixture.svelte";
import ChartHarness from "../../../test/data-display/ChartHarness.svelte";
import { serverRender } from "../../../test/data-display/server-render.js";

let mounted = [];
let styles = [];
// happy-dom exposes locally declared custom properties only. A computed
// longhand resolves inherited var() values, as in technical-drawing.dom.test.js.
function resolvedRole(element, role) {
  const previous = element.style.fontFamily;
  element.style.fontFamily = `var(${role}, missing-ds-role)`;
  const value = getComputedStyle(element).fontFamily;
  element.style.fontFamily = previous;
  return value;
}
afterEach(async () => {
  for (const component of mounted) await unmount(component);
  mounted = [];
  for (const style of styles) style.remove();
  styles = [];
  document.body.replaceChildren();
});

it("inherits the nearest explicit theme without changing sibling contract defaults", async () => {
  const require = createRequire(import.meta.url);
  const style = document.createElement("style");
  style.textContent = ["contract", "technical-drawing"]
    .map((name) =>
      readFileSync(
        require.resolve(`@homelab/design-system/tokens/${name}.css`),
        "utf8",
      ),
    )
    .join("\n");
  document.head.append(style);
  styles.push(style);
  const target = await render(Fixture);
  const light = target.querySelector(
    '[data-ds-theme="technical-drawing-light"]',
  );
  const dark = target.querySelector('[data-ds-theme="technical-drawing-dark"]');
  for (const boundary of [light, dark]) {
    for (const role of [
      "--ds-ink",
      "--ds-surface",
      "--ds-ok",
      "--ds-warn",
      "--ds-err",
    ]) {
      expect(resolvedRole(boundary.querySelector(".metric"), role)).toBe(
        resolvedRole(boundary, role),
      );
      expect(resolvedRole(boundary.querySelector("section"), role)).toBe(
        resolvedRole(boundary, role),
      );
    }
  }
  expect(resolvedRole(light.querySelector(".metric"), "--ds-ink")).not.toBe(
    resolvedRole(dark.querySelector(".metric"), "--ds-ink"),
  );
  const sibling = await render(Metric, { label: "Outside", value: 0 });
  expect(resolvedRole(sibling.querySelector(".metric"), "--ds-ink")).toBe(
    resolvedRole(document.documentElement, "--ds-ink"),
  );
  expect(resolvedRole(sibling.querySelector(".metric"), "--ds-series-1")).toBe(
    "missing-ds-role",
  );
});

async function render(Component, props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  mounted.push(mount(Component, { target, props }));
  await tick();
  return target;
}

it("mounts native headings and semantic left-flowing definition-list rows", async () => {
  expect(Section).toBe(Panel);
  const target = await render(Fixture);
  const section = target.querySelector("section");
  expect(section.querySelector("h3").id).toBe(
    section.getAttribute("aria-labelledby"),
  );
  expect(section.querySelector("footer").textContent).toBe(
    "Synthetic footer partition",
  );
  for (const density of ["dense", "sparse"]) {
    const dl = target.querySelector(`dl[data-density="${density}"]`);
    expect([...dl.children]).toHaveLength(DATA_DISPLAY_FIXTURES.rows.length);
    for (const row of dl.children)
      expect([...row.children].map(({ tagName }) => tagName)).toEqual([
        "DT",
        "DD",
      ]);
    expect(dl.textContent).toContain(
      "LongSyntheticValueWithoutBreaksForWrapping",
    );
    expect(dl.textContent).toContain(
      "LongSyntheticUnitWithoutBreaksForWrapping",
    );
    expect(dl.querySelectorAll("dd")[2].textContent).toBe("0 requests");
    expect(dl.querySelectorAll("dd")[3].textContent).toContain("Unavailable");
  }
  const defaultHeading = await render(Panel, { title: "Default heading" });
  expect(defaultHeading.querySelector("h2").textContent).toBe(
    "Default heading",
  );
  const empty = await render(KeyValue);
  expect(empty.querySelector("dl").children).toHaveLength(0);
});

it("keeps every status label visible with distinct hidden cues and no default announcements", async () => {
  const cues = [];
  for (const [kind, status] of Object.entries(STATUS_KINDS)) {
    const target = await render(Status, {
      kind,
      label: `Human label: ${status.label}`,
    });
    const node = target.querySelector(".status");
    const cue = node.querySelector('[aria-hidden="true"]');
    cues.push(cue.textContent);
    expect(node.lastElementChild.textContent).toBe(
      `Human label: ${status.label}`,
    );
    expect(node.lastElementChild.hasAttribute("aria-hidden")).toBe(false);
    expect(node.hasAttribute("role")).toBe(false);
    expect(node.hasAttribute("aria-live")).toBe(false);
  }
  expect(new Set(cues).size).toBe(5);
  const live = await render(Status, {
    kind: "pending",
    label: "Loading synthetic data",
    live: true,
  });
  expect(live.querySelector('[role="status"]').getAttribute("aria-live")).toBe(
    "polite",
  );
});

it("stays keyboard-usable with a fallback focus ring outside a boundary", async () => {
  // No boundary and no stylesheets: unsupported per the README, but the
  // disclosure must stay keyboard-usable. The unit suite asserts both
  // summaries ship a currentColor focus fallback in their scoped CSS.
  const target = await render(Metric, { label: "Outside", value: 1 });
  const harness = await render(ChartHarness);
  expect(harness.querySelector("figure")).not.toBeNull();
  const summary = target.querySelector("summary");
  summary.focus();
  expect(document.activeElement).toBe(summary);
  summary.click();
  expect(target.querySelector("details").open).toBe(true);
  expect(target.querySelector("data").textContent).toBe("1");
  const status = await render(Status, { kind: "ok", label: "Outside ok" });
  expect(status.querySelector(".status").textContent).toContain("Outside ok");
});

it("exposes exact measurements in accessible text and a native touch/keyboard disclosure", async () => {
  const target = await render(Metric, {
    label: "Bytes",
    value: 999950,
    unit: "bytes",
    context: "Synthetic",
  });
  expect(target.querySelector(".label").textContent).toBe("Bytes");
  expect(
    target.querySelector('[role="group"]').getAttribute("aria-labelledby"),
  ).toBe(target.querySelector(".label").id);
  expect(target.querySelector(".measurement [aria-hidden]").textContent).toBe(
    "1M bytes",
  );
  const exact = target.querySelector(".measurement .exact-sr");
  expect(exact.textContent).toBe("999,950 bytes");
  expect(exact.closest('[aria-hidden="true"]')).toBeNull();
  const summary = target.querySelector("summary");
  expect(summary.textContent).toBe("Exact value");
  summary.focus();
  expect(document.activeElement).toBe(summary);
  summary.click();
  expect(target.querySelector("details").open).toBe(true);
  expect(target.querySelector("details data").textContent).toBe(
    "999,950 bytes",
  );
  expect(target.querySelector("data").getAttribute("value")).toBe("999950");
  expect(target.querySelector(".context").textContent).toBe("Synthetic");
  expect(target.querySelector("[title]")).toBeNull();
});

it.each(
  DATA_DISPLAY_FIXTURES.measurements.filter(
    ({ value }) => typeof value === "number" && Number.isFinite(value),
  ),
)(
  "renders $label as a real measurement with exact value and units",
  async ({ label, value, unit }) => {
    const target = await render(Metric, { label, value, unit });
    const formatted = formatMeasurement(value, { unit });
    expect(target.querySelector(".metric").dataset.state).toBe("ready");
    expect(target.querySelector(".state")).toBeNull();
    expect(target.querySelector(".measurement [aria-hidden]").textContent).toBe(
      `${formatted.text} ${unit}`,
    );
    expect(target.querySelector(".exact-sr").textContent).toBe(
      formatted.exactText,
    );
    expect(target.querySelector("data").getAttribute("value")).toBe(
      String(value),
    );
  },
);

it.each(
  DATA_DISPLAY_FIXTURES.measurements.filter(
    ({ value }) => typeof value !== "number" || !Number.isFinite(value),
  ),
)(
  "renders $label as Unavailable, never zero or an exact-value disclosure",
  async ({ label, value, unit }) => {
    const target = await render(Metric, { label, value, unit });
    expect(target.querySelector(".state").textContent).toBe("Unavailable");
    expect(target.querySelector(".metric").dataset.state).toBe("unavailable");
    expect(target.querySelector(".measurement, data, details")).toBeNull();
    expect(target.querySelector('[data-kind="ok"]')).toBeNull();
  },
);

it.each(DATA_DISPLAY_FIXTURES.states)(
  "shows %s state text instead of stale measurements",
  async (state) => {
    const target = await render(Metric, { label: "State", value: 0, state });
    expect(target.querySelector(".state").textContent.toLowerCase()).toBe(
      state,
    );
    expect(target.querySelector("details, .measurement")).toBeNull();
    expect(target.querySelector(".metric").getAttribute("aria-busy")).toBe(
      state === "loading" ? "true" : null,
    );
    const panel = await render(Panel, { title: "State panel", state });
    expect(panel.querySelector(".state").textContent.toLowerCase()).toBe(state);
  },
);

it.each(["ready", ...DATA_DISPLAY_FIXTURES.states])(
  "retains supplied units when a measurement is %s or missing",
  async (state) => {
    const target = await render(Metric, {
      label: "Memory",
      value: null,
      unit: "bytes",
      state,
    });
    expect(target.querySelector(".unit-state").textContent).toBe(
      "Units: bytes",
    );
    expect(target.querySelector(".measurement, details")).toBeNull();
    expect(target.querySelector(".state").textContent).toBe(
      state === "ready"
        ? "Unavailable"
        : `${state[0].toUpperCase()}${state.slice(1)}`,
    );
  },
);

it.each(["en-US", "de-DE"])(
  "hydrates %s SSR without replacing nodes, text or attributes",
  async (locale) => {
    const { body } = await serverRender(undefined, { locale });
    const target = document.createElement("div");
    target.innerHTML = body;
    document.body.append(target);
    const elements = [...target.querySelectorAll("*")];
    const text = target.textContent;
    const attributes = () =>
      [...target.querySelectorAll("*")].map((node) =>
        [...node.attributes].map(({ name, value }) => [name, value]),
      );
    const before = attributes();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      mounted.push(
        hydrate(Fixture, { target, props: { locale }, recover: false }),
      );
      await tick();
      expect(target.textContent).toBe(text);
      expect(attributes()).toEqual(before);
      expect([...target.querySelectorAll("*")]).toEqual(elements);
      expect(warn.mock.calls).toEqual([]);
      expect(error.mock.calls).toEqual([]);
      const summary = target.querySelector("summary");
      summary.click();
      expect(target.querySelector("details").open).toBe(true);
    } finally {
      warn.mockRestore();
      error.mockRestore();
    }
  },
);
