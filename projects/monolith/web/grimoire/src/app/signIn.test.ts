import { afterEach, describe, expect, it, vi } from "vitest";
import { signIn } from "./signIn";

afterEach(() => {
  vi.useRealTimers();
  signIn.reset();
});

/** Two signals more than two seconds apart confirm an expired sign-in. */
function expire() {
  vi.useFakeTimers();
  signIn.signal();
  vi.advanceTimersByTime(2_500);
  signIn.signal();
}

describe("signIn", () => {
  it("is reconnecting after one signal and live again after an answer", () => {
    signIn.signal();
    expect(signIn.state()).toBe("reconnecting");
    signIn.answered();
    expect(signIn.state()).toBe("live");
  });

  it("never reloads a screen that has not said how to save its work", () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });
    expire();
    expect(reload).not.toHaveBeenCalled();
    expect(signIn.state()).toBe("signed-out");
    vi.unstubAllGlobals();
  });

  it("holds the reload while a hook says work is in flight", () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });
    signIn.beforeReload(() => false);
    expire();
    expect(reload).not.toHaveBeenCalled();
    expect(signIn.state()).toBe("reconnecting");
    vi.unstubAllGlobals();
  });
});
