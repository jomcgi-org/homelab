// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Launcher from "./Launcher.svelte";

const mounted = [];

async function render(onSubmit) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Launcher, {
    target,
    props: {
      session: {
        prompt: "Start this task",
        model: "",
        repo: "",
        branch: "",
      },
      models: ["luna"],
      summary: {
        items: [],
        count: 0,
        allCount: 0,
        sessionCount: 0,
        spend: 0,
      },
      onSubmit,
    },
  });
  mounted.push({ component, target });
  await tick();
  return target;
}

afterEach(async () => {
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("launcher submit path", () => {
  test("form submit and command enter call the provided task creator", async () => {
    const createTask = vi.fn();
    const target = await render(createTask);

    target
      .querySelector("form")
      .dispatchEvent(new Event("submit", { bubbles: true }));
    target.querySelector("textarea").dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        metaKey: true,
        bubbles: true,
      }),
    );
    await tick();

    expect(createTask).toHaveBeenCalledTimes(2);
  });
});
