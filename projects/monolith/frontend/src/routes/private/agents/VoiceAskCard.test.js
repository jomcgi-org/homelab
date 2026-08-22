// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import VoiceAskCard from "./VoiceAskCard.svelte";

const mounted = [];

afterEach(async () => {
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("voice ask card", () => {
  test("sends a clicked option once through the injected composer function", async () => {
    const target = document.createElement("div");
    document.body.append(target);
    const onSend = vi.fn().mockResolvedValue(undefined);
    const component = mount(VoiceAskCard, {
      target,
      props: {
        card: {
          key: "ask:7",
          question: "Ship it?",
          ref: "run-84",
          options: ["approve", "send back"],
          answered: false,
        },
        sessionId: 213,
        onSend,
        onAnswered: vi.fn(),
      },
    });
    mounted.push({ component, target });

    target.querySelector("button").click();
    await tick();
    await vi.waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend).toHaveBeenCalledWith({
      session_id: 213,
      prompt: "approve",
    });
  });
});
