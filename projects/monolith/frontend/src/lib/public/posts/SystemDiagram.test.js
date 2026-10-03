// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import SystemDiagram from "./SystemDiagram.svelte";
let component;
let target;
async function render(mode) {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(SystemDiagram, {
    target,
    props: {
      mode,
      title: "System",
      notes: [
        { key: "1", html: "Original explanation" },
        { key: "2", html: "Read the selected rows" },
        { key: "A", html: "GPU weights" },
      ],
    },
  });
  await tick();
  return target;
}
afterEach(async () => {
  if (component) await unmount(component);
  target?.remove();
  vi.useRealTimers();
});

test("a cold cache hit runs through the CPU and adds NVMe only on a miss", async () => {
  const view = await render("memory");
  view.querySelectorAll(".paths button")[2].click();
  await tick();
  expect(view.querySelector(".path-description").textContent).toContain("CPU");
  expect(view.querySelector(".path-description").textContent).not.toContain(
    "NVMe",
  );
  view.querySelectorAll(".paths button")[3].click();
  await tick();
  expect(view.querySelector(".path-description").textContent).toContain("NVMe");
});

test("selecting a step reveals original prose without extra animation controls", async () => {
  const view = await render("prefill");
  expect(view.querySelector(".explanation").textContent).toContain(
    "Original explanation",
  );
  view.querySelectorAll(".steps button")[1].click();
  await tick();
  expect(view.querySelector(".explanation").textContent).toContain(
    "Read the selected rows",
  );
  expect(view.textContent).not.toContain("Animate flow");
  expect(view.textContent).not.toContain("Illustrated execution");
  expect(view.querySelector("details").textContent).toContain(
    "Original explanation",
  );
});

test("prefill changes from row selection to transfer to grouped compute", async () => {
  const view = await render("prefill");
  const tokens = [...view.querySelectorAll(".token")].map((token) =>
    token.getAttribute("style"),
  );
  expect(view.querySelectorAll("svg .traveller")).toHaveLength(12);
  view.querySelectorAll(".steps button")[1].click();
  await tick();
  expect(view.querySelectorAll("svg .traveller")).toHaveLength(3);
  expect(view.querySelectorAll("svg .row-data")).toHaveLength(12);
  view.querySelectorAll(".steps button")[2].click();
  await tick();
  expect(
    [...view.querySelectorAll(".token")].map((token) =>
      token.getAttribute("style"),
    ),
  ).not.toEqual(tokens);
  expect(view.querySelectorAll("svg .batch")).toHaveLength(9);
});

test("decode routes first, classifies residency, moves one chosen path, then combines", async () => {
  const view = await render("decode");
  expect(view.querySelectorAll("svg .traveller")).toHaveLength(4);
  view.querySelectorAll(".steps button")[1].click();
  await tick();
  expect(view.querySelectorAll("svg .residency")).toHaveLength(3);
  expect(view.querySelectorAll("svg .traveller")).toHaveLength(0);
  view.querySelectorAll(".steps button")[2].click();
  await tick();
  view.querySelectorAll(".paths button")[2].click();
  await tick();
  expect(view.querySelector("svg").textContent).toContain("Page cache");
  view.querySelectorAll(".paths button")[3].click();
  await tick();
  expect(view.querySelector("svg").textContent).toContain("NVMe read");
  view.querySelectorAll(".steps button")[3].click();
  await tick();
  expect(view.querySelectorAll("svg .traveller")).toHaveLength(4);
});

test("slot replacement leaves the incumbent serving until the mapping flip", async () => {
  const view = await render("swap");
  expect(view.querySelector(".replacement")).toBeNull();
  view.querySelectorAll(".steps button")[1].click();
  await tick();
  expect(view.querySelector(".replacement")).not.toBeNull();
  expect(
    view.querySelector(".incumbent").parentElement.classList.contains("muted"),
  ).toBe(false);
  const oldPath = [...view.querySelectorAll("svg .traveller")]
    .at(-1)
    .getAttribute("d");
  view.querySelectorAll(".steps button")[2].click();
  await tick();
  expect(
    view.querySelector(".incumbent").parentElement.classList.contains("muted"),
  ).toBe(true);
  expect(
    [...view.querySelectorAll("svg .traveller")].at(-1).getAttribute("d"),
  ).not.toBe(oldPath);
  expect(view.querySelector("svg").textContent).toContain("New expert serves");
});
