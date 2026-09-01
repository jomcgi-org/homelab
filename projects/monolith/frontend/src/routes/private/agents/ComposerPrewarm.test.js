// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ComposerPrewarm from "./ComposerPrewarm.svelte";

let mounted;

async function render(sessionId) {
  const target = document.createElement("div");
  target.innerHTML = '<form class="composer"><textarea></textarea></form>';
  document.body.append(target);
  const component = mount(ComposerPrewarm, {
    target,
    props: { sessionId },
  });
  mounted = { component, target };
  await tick();
  return target.querySelector("textarea");
}

function type(textarea, value) {
  textarea.value = value;
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

afterEach(async () => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  if (mounted) {
    await unmount(mounted.component);
    mounted.target.remove();
    mounted = null;
  }
});

describe("composer prewarm", () => {
  test("posts once per typing burst and rearms after 30 seconds idle", async () => {
    vi.useFakeTimers();
    globalThis.fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const textarea = await render(42);

    type(textarea, "h");
    vi.advanceTimersByTime(20_000);
    type(textarea, "he");
    vi.advanceTimersByTime(29_999);
    type(textarea, "hel");

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(globalThis.fetch).toHaveBeenCalledWith("/private/agents/prewarm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: 42 }),
    });

    vi.advanceTimersByTime(30_000);
    type(textarea, "hell");

    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });

  test("does not post for the new-session composer", async () => {
    globalThis.fetch = vi.fn();
    const textarea = await render(null);

    type(textarea, "new session");

    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});
