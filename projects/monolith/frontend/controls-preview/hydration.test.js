// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { hydrate, tick, unmount } from "svelte";
import ControlsFixture from "./ControlsFixture.svelte";
import { renderFixture } from "./server-render.js";

let mounted;
let target;
let warn;
let error;
afterEach(async () => {
  try {
    if (mounted) await unmount(mounted);
    if (warn) expect(warn.mock.calls).toEqual([]);
    if (error) expect(error.mock.calls).toEqual([]);
  } finally {
    target?.remove();
    mounted = undefined;
    warn = undefined;
    error = undefined;
    vi.restoreAllMocks();
  }
});

async function setup(props = {}) {
  const { body } = await renderFixture(props);
  target = document.createElement("div");
  target.innerHTML = body;
  document.body.append(target);
  const nodes = [...target.querySelectorAll("*")];
  const text = target.textContent;
  const attributes = () =>
    [...target.querySelectorAll("*")].map((node) =>
      [...node.attributes].map(({ name, value }) => [name, value]),
    );
  const before = attributes();
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  error = vi.spyOn(console, "error").mockImplementation(() => {});
  mounted = hydrate(ControlsFixture, { target, props, recover: false });
  await tick();
  const after = [...target.querySelectorAll("*")];
  expect(after).toHaveLength(nodes.length);
  after.forEach((node, i) => expect(node).toBe(nodes[i]));
  expect(target.textContent).toBe(text);
  expect(attributes()).toEqual(before);
  expect(warn.mock.calls).toEqual([]);
  expect(error.mock.calls).toEqual([]);
  return target.querySelector('[data-sample="light"]');
}

async function press(node, key) {
  node.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
  );
  await tick();
}

function assertTabs(root, index) {
  const tabs = [...root.querySelectorAll('[role="tab"]')];
  const panels = [...root.querySelectorAll('[role="tabpanel"]')];
  expect(tabs.filter((tab) => tab.tabIndex === 0)).toHaveLength(1);
  tabs.forEach((tab, i) => {
    expect(tab.type).toBe("button");
    expect(tab.getAttribute("aria-controls")).toBe(panels[i].id);
    expect(panels[i].getAttribute("aria-labelledby")).toBe(tab.id);
    expect(panels[i].tabIndex).toBe(0);
    expect(panels[i].hidden).toBe(i !== index);
    expect(tab.getAttribute("aria-selected")).toBe(String(i === index));
    expect(tab.tabIndex).toBe(i === index ? 0 : -1);
  });
  return tabs;
}

it("hydrates every component with stable nodes, attributes, ids and working handlers", async () => {
  const root = await setup();
  const ids = [...target.querySelectorAll("[id]")].map((node) => node.id);
  expect(new Set(ids).size).toBe(ids.length);
  root.querySelector('[data-action="header"]').click();
  await tick();
  expect(root.querySelector('[data-action="default"]').textContent).toContain(
    "Sample action: 1",
  );
});

it("keeps explicit button types, forwarded form attributes, names and native disabled semantics", async () => {
  const root = await setup();
  const button = root.querySelector('[data-action="default"]');
  expect(button.type).toBe("button");
  expect(button.getAttribute("type")).toBe("button");
  expect(button.name).toBe("action");
  expect(button.value).toBe("sample");
  expect(root.querySelector('[data-action="submit"]').type).toBe("submit");
  expect(root.querySelector('[type="reset"]').type).toBe("reset");
  const external = root.querySelector('[data-action="external-submit"]');
  expect(external.form).toBe(root.querySelector("form"));
  expect(
    root.querySelector('[data-action="named"]').getAttribute("aria-label"),
  ).toBe("Named action");
  button.click();
  await tick();
  expect(button.textContent).toContain("Sample action: 1");
  expect(root.querySelector('[data-state="submissions"]').textContent).toBe(
    "Submissions: 0",
  );
  const disabled = root.querySelector('[data-action="disabled"]');
  expect(disabled.disabled).toBe(true);
  disabled.click();
  await tick();
  expect(button.textContent).toContain("Sample action: 1");
  root.querySelector('[data-action="named"]').click();
  await tick();
  expect(button.textContent).toContain("Sample action: 2");
  root.querySelector('[data-action="submit"]').click();
  await tick();
  expect(root.querySelector('[data-state="submissions"]').textContent).toBe(
    "Submissions: 1",
  );
});

it("associates labels, descriptions, errors and native required/disabled state", async () => {
  const root = await setup();
  const select = root.querySelector("select");
  const label = [...root.querySelectorAll("label")].find(
    (node) => node.htmlFor === select.id,
  );
  expect(label.textContent).toContain("Sample region (required)");
  const describedby = select.getAttribute("aria-describedby").split(" ");
  expect(describedby).toHaveLength(2);
  expect(
    describedby.map((id) => document.getElementById(id).textContent),
  ).toEqual(["Select a synthetic region", "Error: Choose another region"]);
  expect(select.getAttribute("aria-invalid")).toBe("true");
  expect(select.required).toBe(true);
  expect(select.disabled).toBe(false);
  const input = root.querySelector("input");
  expect(input.disabled).toBe(true);
  expect(input.required).toBe(false);
  expect(input.hasAttribute("aria-describedby")).toBe(false);
  expect(input.hasAttribute("aria-invalid")).toBe(false);
  root.querySelector('[data-action="field"]').click();
  await tick();
  expect(select.disabled).toBe(true);
  expect(select.id).toBe(label.htmlFor);
});

it("renders native disclosure and synchronizes open binding in both directions", async () => {
  const root = await setup();
  const details = root.querySelector("details");
  expect(details.firstElementChild.tagName).toBe("SUMMARY");
  expect(details.querySelector("summary").textContent).toContain(
    "Sample disclosure",
  );
  expect(details.querySelector('[aria-hidden="true"]')).not.toBeNull();
  expect(details.open).toBe(false);
  root.querySelector('[data-action="disclosure"]').click();
  await tick();
  expect(details.open).toBe(true);
  // happy-dom does not implement summary's native keyboard/click toggle.
  details.open = false;
  details.dispatchEvent(new Event("toggle"));
  await tick();
  expect(root.querySelector('[data-state="disclosure"]').textContent).toBe(
    "Open: false",
  );
});

it("maintains tab/panel relationships, automatic wraparound and disabled skipping over repeated cycles", async () => {
  const root = await setup();
  const tablist = root.querySelector('[role="tablist"]');
  expect(
    document.getElementById(tablist.getAttribute("aria-labelledby"))
      .textContent,
  ).toBe("Sample panels");
  expect(tablist.getAttribute("aria-orientation")).toBe("horizontal");
  const tabs = assertTabs(root, 0);
  tabs[0].focus();
  for (let cycle = 0; cycle < 3; cycle++) {
    for (const [key, index] of [
      ["ArrowLeft", 3],
      ["ArrowRight", 0],
      ["ArrowRight", 2],
      ["End", 3],
      ["Home", 0],
    ]) {
      await press(document.activeElement, key);
      assertTabs(root, index);
      expect(document.activeElement).toBe(tabs[index]);
    }
  }
  expect(root.querySelector('[data-state="selection"]').textContent).toBe(
    "Selected: overview; changes: 15",
  );
  tabs[1].click();
  await tick();
  assertTabs(root, 0);
  tabs[2].click();
  await tick();
  assertTabs(root, 2);
  expect(root.querySelector('[data-state="selection"]').textContent).toBe(
    "Selected: metrics; changes: 16",
  );
  root.querySelector('[data-action="selection"]').click();
  await tick();
  assertTabs(root, 3);
  expect(root.querySelector('[data-state="selection"]').textContent).toBe(
    "Selected: logs; changes: 16",
  );
});

it("supports vertical arrows and ignores arrows belonging to the other orientation", async () => {
  const root = await setup({ orientation: "vertical" });
  const tabs = assertTabs(root, 0);
  expect(
    root.querySelector('[role="tablist"]').getAttribute("aria-orientation"),
  ).toBe("vertical");
  tabs[0].focus();
  await press(tabs[0], "ArrowRight");
  assertTabs(root, 0);
  await press(tabs[0], "ArrowUp");
  expect(document.activeElement).toBe(tabs[3]);
  assertTabs(root, 3);
  await press(tabs[3], "ArrowDown");
  assertTabs(root, 0);
});

it("selects automatically on focus and emits one change per new selection", async () => {
  const root = await setup();
  const tabs = assertTabs(root, 0);
  tabs[2].focus();
  await tick();
  assertTabs(root, 2);
  expect(root.querySelector('[data-state="selection"]').textContent).toBe(
    "Selected: metrics; changes: 1",
  );
  tabs[2].click();
  await tick();
  expect(root.querySelector('[data-state="selection"]').textContent).toBe(
    "Selected: metrics; changes: 1",
  );
});

it("keeps one stop for a single tab without duplicate change callbacks", async () => {
  const root = await setup({
    tabs: [{ id: "only", label: "Only" }],
    initialSelected: "only",
  });
  const [tab] = assertTabs(root, 0);
  tab.focus();
  for (const key of ["ArrowLeft", "ArrowRight", "Home", "End"]) {
    await press(tab, key);
    expect(document.activeElement).toBe(tab);
    assertTabs(root, 0);
  }
  tab.click();
  await tick();
  expect(root.querySelector('[data-state="selection"]').textContent).toBe(
    "Selected: only; changes: 0",
  );
});

it.each(["unavailable", "missing"])(
  "falls back to the first enabled tab for selection %s",
  async (initialSelected) => {
    const root = await setup({ initialSelected });
    assertTabs(root, 0);
  },
);

it.each([
  { tabs: [] },
  { tabs: [{ id: "disabled", label: "Disabled", disabled: true }] },
])(
  "exposes no tab stop or visible panel when no tab is enabled: %j",
  async ({ tabs }) => {
    const root = await setup({ tabs });
    expect(
      [...root.querySelectorAll('[role="tab"]')].filter(
        (tab) => tab.tabIndex === 0,
      ),
    ).toHaveLength(0);
    expect(
      [...root.querySelectorAll('[role="tabpanel"]')].filter(
        (panel) => !panel.hidden,
      ),
    ).toHaveLength(0);
  },
);

it.each([1, 2, 3, 4, 5, 6])(
  "renders supported heading level %i",
  async (headingLevel) => {
    const root = await setup({ headingLevel });
    expect(root.querySelector(`header h${headingLevel}`)).not.toBeNull();
  },
);

it("renders wrapping breadcrumb names, current page, heading level and action snippet", async () => {
  const root = await setup({ headingLevel: 3 });
  const header = root.querySelector("header");
  expect(header.querySelector("h3").textContent).toBe(
    "SyntheticReferenceWithAnUnbrokenNameThatMustWrapAtNarrowWidths",
  );
  expect(header.querySelector("h1, h2, h4, h5, h6")).toBeNull();
  const nav = header.querySelector("nav");
  expect(nav.getAttribute("aria-label")).toBe("Breadcrumb");
  expect(nav.querySelector("ol").children).toHaveLength(2);
  expect(nav.querySelector('[aria-current="page"]').textContent.trim()).toBe(
    header.querySelector("h3").textContent,
  );
  expect(nav.querySelector('[aria-hidden="true"]').textContent).toBe("/");
  expect(header.querySelector('[data-action="header"]')).not.toBeNull();
  expect(target.querySelector('[data-sample="contract"] h1').textContent).toBe(
    "Contract fallback",
  );
  expect(
    target.querySelector('[data-sample="contract"] span[aria-current="page"]')
      .textContent,
  ).toBe("Current page");
  expect(
    target.querySelector('[aria-label="Standalone single panel"]'),
  ).not.toBeNull();
});
