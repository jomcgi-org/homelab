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

test("a cold cache hit runs through the CPU and enables disk only on a miss", async () => {
  const view = await render("memory");
  view.querySelectorAll(".diagram-controls button")[2].click();
  await tick();
  expect(view.querySelector(".node.cpu").classList.contains("active")).toBe(
    true,
  );
  expect(view.querySelector(".node.disk").classList.contains("active")).toBe(
    false,
  );
  expect(view.querySelector(".diagram-explanation").textContent).toContain(
    "already in the page cache",
  );
  view.querySelectorAll(".diagram-controls button")[3].click();
  await tick();
  expect(view.querySelector(".node.disk").classList.contains("active")).toBe(
    true,
  );
  expect(view.querySelector(".diagram-explanation").textContent).toContain(
    "from NVMe",
  );
});

test("sequence playback reveals authored steps and stops when a step is selected", async () => {
  vi.useFakeTimers();
  const view = await render("prefill");
  expect(view.querySelector(".diagram-explanation").textContent).toContain(
    "Original explanation",
  );
  view.querySelector(".diagram-heading button").click();
  await tick();
  vi.advanceTimersByTime(2400);
  await tick();
  expect(view.querySelector(".diagram-explanation").textContent).toContain(
    "Read the selected rows",
  );
  view.querySelector(".diagram-controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(0);
  expect(
    view.querySelector(".diagram-heading button").getAttribute("aria-pressed"),
  ).toBe("false");
});
