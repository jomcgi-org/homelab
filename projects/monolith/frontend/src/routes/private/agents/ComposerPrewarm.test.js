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
  test("fires immediately, then every 15 seconds while typing until 30 seconds idle", async () => {
    vi.useFakeTimers();
    globalThis.fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const textarea = await render(42);

    type(textarea, "h");
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(globalThis.fetch).toHaveBeenCalledWith("/private/agents/prewarm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: 42 }),
    });

    vi.advanceTimersByTime(5_000);
    type(textarea, "he");
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);

    // The original interval still fires at t=15s. Input only resets the
    // 30-second stop deadline; it does not restart the interval.
    vi.advanceTimersByTime(10_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(5_000);
    type(textarea, "hel");
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);

    // The latest input moved the stop deadline to t=50s, so the interval is
    // still live at t=30s and t=45s.
    vi.advanceTimersByTime(10_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(3);

    vi.advanceTimersByTime(5_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(3);

    vi.advanceTimersByTime(10_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(4);

    // At t=50s the stop timer clears the interval. Its next t=60s tick and all
    // later ticks must remain suppressed.
    vi.advanceTimersByTime(5_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(4);

    vi.advanceTimersByTime(10_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(4);

    vi.advanceTimersByTime(30_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(4);
  });

  test("does not post for the new-session composer", async () => {
    globalThis.fetch = vi.fn();
    const textarea = await render(null);

    type(textarea, "new session");

    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});
