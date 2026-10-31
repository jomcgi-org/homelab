// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";

vi.mock("$app/navigation", () => ({
  invalidateAll: vi.fn(),
  replaceState: vi.fn(),
}));
let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  document.body.innerHTML = "";
});

it("renders only current execution in the running section and keeps pauses visible", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const task = (issue_number, state) => ({
    issue_number,
    state,
    title: `Task ${issue_number}`,
    admitted_at: "2026-10-19T08:00:00Z",
    nodes: [],
  });
  instance = mount(Page, {
    target,
    props: {
      data: {
        board: {
          snapshotted_at: new Date().toISOString(),
          state: "enabled",
          policy: { max_tasks: 10 },
          active: [
            task(1, "executing"),
            ...[2, 3, 4, 5].map((id) => task(id, "paused")),
            task(6, "uncertain"),
          ],
          queued: [],
          recent: [],
        },
      },
    },
  });
  await tick();
  const sections = [...target.querySelectorAll("section.panel")];
  expect(sections[0].querySelectorAll("a.row")).toHaveLength(1);
  expect(sections[1].querySelectorAll("a.row")).toHaveLength(4);
  expect(sections[2].querySelectorAll("a.row")).toHaveLength(1);
  expect(target.textContent).toContain("Task age");
  expect(target.querySelectorAll(".stats .v")[1].textContent.trim()).toBe("1");
  const pausedChip = [...target.querySelectorAll(".chips button")].find(
    (el) => el.textContent.trim() === "paused",
  );
  pausedChip.click();
  await tick();
  expect(sections[0].querySelectorAll("a.row")).toHaveLength(0);
  expect(sections[1].querySelectorAll("a.row")).toHaveLength(4);
});
