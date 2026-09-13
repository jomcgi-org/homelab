import { describe, expect, test, vi } from "vitest";
import { nextStatus, streamAge } from "./vm-stream-status.js";

const initial = () => ({ mode: "connecting", lastUpdateAt: null, error: null });

describe("VM stream status", () => {
  test("stays connecting when the initial event is unknown", () => {
    expect(nextStatus(undefined, { type: "unknown" })).toEqual(initial());
  });

  test("moves from initial connecting to streaming when the stream opens", () => {
    expect(nextStatus(undefined, { type: "open" }).mode).toBe("streaming");
  });

  test("moves from initial connecting to streaming on a successful frame", () => {
    expect(nextStatus(undefined, { type: "frame", at: 1_000 })).toEqual({
      mode: "streaming",
      lastUpdateAt: 1_000,
      error: null,
    });
  });

  test("moves from initial connecting to stalled on an error frame", () => {
    expect(
      nextStatus(undefined, {
        type: "frame",
        at: 1_000,
        error: "control plane unreachable",
      }),
    ).toEqual({
      mode: "stalled",
      lastUpdateAt: null,
      error: "control plane unreachable",
    });
  });

  test("moves from initial connecting to polling when fallback is armed", () => {
    expect(nextStatus(undefined, { type: "fallback-armed" }).mode).toBe(
      "polling",
    );
  });

  test("moves from open to a successful streaming frame", () => {
    const opened = nextStatus(initial(), { type: "open" });
    const framed = nextStatus(opened, { type: "frame", at: 1_000 });

    expect(opened.mode).toBe("streaming");
    expect(framed).toEqual({
      mode: "streaming",
      lastUpdateAt: 1_000,
      error: null,
    });
  });

  test("treats an error payload as stalled even though a frame arrived", () => {
    const status = nextStatus(
      { mode: "streaming", lastUpdateAt: 1_000, error: null },
      { type: "frame", at: 2_000, error: "control plane unreachable" },
    );

    expect(status).toEqual({
      mode: "stalled",
      lastUpdateAt: 1_000,
      error: "control plane unreachable",
    });
  });

  test("moves to polling when the fallback is armed", () => {
    expect(nextStatus(initial(), { type: "fallback-armed" }).mode).toBe(
      "polling",
    );
  });

  test("stalls after the stream closes and a fallback poll fails", () => {
    const closed = nextStatus(
      { mode: "streaming", lastUpdateAt: 1_000, error: null },
      { type: "closed" },
    );
    const failed = nextStatus(closed, {
      type: "poll-fail",
      error: "request failed",
    });

    expect(closed.mode).toBe("stalled");
    expect(failed.mode).toBe("stalled");
  });

  test("recovers from a failed poll", () => {
    const failed = nextStatus(initial(), {
      type: "poll-fail",
      error: "request failed",
    });
    const recovered = nextStatus(failed, { type: "poll-ok", at: 2_000 });

    expect(recovered).toEqual({
      mode: "polling",
      lastUpdateAt: 2_000,
      error: null,
    });
  });

  test("treats an error in a successful poll response as stalled", () => {
    const status = nextStatus(initial(), {
      type: "poll-ok",
      at: 2_000,
      error: "control plane unreachable",
    });

    expect(status).toEqual({
      mode: "stalled",
      lastUpdateAt: null,
      error: "control plane unreachable",
    });
  });

  test("advances the update time only for successful frames and polls", () => {
    const framed = nextStatus(initial(), { type: "frame", at: 1_000 });
    const errorFrame = nextStatus(framed, {
      type: "frame",
      at: 2_000,
      error: "control plane unreachable",
    });
    const failedPoll = nextStatus(errorFrame, {
      type: "poll-fail",
      at: 3_000,
      error: "request failed",
    });
    const successfulPoll = nextStatus(failedPoll, {
      type: "poll-ok",
      at: 4_000,
    });

    expect(errorFrame.lastUpdateAt).toBe(1_000);
    expect(failedPoll.lastUpdateAt).toBe(1_000);
    expect(successfulPoll.lastUpdateAt).toBe(4_000);
  });

  test("stays streaming through a long idle period with no frames", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-08-14T00:00:00Z"));
      const status = nextStatus(initial(), { type: "open" });
      vi.advanceTimersByTime(45 * 60_000);

      expect(status.mode).toBe("streaming");
      expect(status.lastUpdateAt).toBe(null);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("streamAge", () => {
  test.each([
    [0, "0s"],
    [59_000, "59s"],
    [60_000, "1m"],
    [59 * 60_000, "59m"],
    [60 * 60_000, "1h"],
    [23 * 60 * 60_000, "23h"],
    [24 * 60 * 60_000, "1d"],
    [48 * 60 * 60_000, "2d"],
  ])("formats %i milliseconds as %s", (milliseconds, expected) => {
    expect(streamAge(milliseconds)).toBe(expected);
  });
});
