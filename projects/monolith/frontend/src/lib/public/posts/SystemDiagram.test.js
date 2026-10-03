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
