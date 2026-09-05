// @vitest-environment happy-dom
import { afterEach, expect, test } from "vitest";
import { mount, tick, unmount } from "svelte";
import Turns from "./Turns.svelte";

const mounted = [];

afterEach(async () => {
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

test("does not mark an interrupted turn as failed or duplicate its pending prompt", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Turns, {
    target,
    props: {
      detail: {
        turns: [
          {
            seq: 1,
            prompt: "continue",
            result_text: "The VM was preempted.",
            terminal_reason: "interrupted",
            stop_reason: "brick_preempted",
            created_at: "2026-09-04T12:00:00Z",
          },
        ],
        pending_queue: [
          {
            seq: 1,
            prompt: "continue",
            created_at: "2026-09-04T12:00:00Z",
          },
        ],
      },
      selectedSession: { model: "luna" },
    },
  });
  mounted.push({ component, target });
  await tick();

  expect(target.querySelector(".badge-failed")).toBeNull();
  expect(target.querySelector(".turn-error")).toBeNull();
  expect(target.textContent).toContain("The VM was preempted.");
  expect(target.querySelectorAll(".prompt-text")).toHaveLength(1);
});
