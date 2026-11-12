import { describe, expect, it, vi } from "vitest";
import * as moving from "../routes/friends/moving/chat-stream.js";
import * as grimoire from "./public/grimoire/chat/stream.js";

for (const [name, caller] of [
  ["moving", moving],
  ["grimoire", grimoire],
]) {
  const invoke = (options) =>
    name === "moving"
      ? caller.streamChatMessage(
          "question",
          [{ role: "user", content: "prior" }],
          options,
        )
      : caller.streamChatMessage("question", options);

  describe(`${name} stream caller`, () => {
    it("delivers mixed framing, split UTF-8 and EOF through its reducer", async () => {
      const events = [
        { type: "node_touched", data: { id: 1, title: "grounding" } },
        { type: "busy", data: { message: "retry" } },
        { type: "token", data: { text: "雪🙂" } },
        { type: "error", data: { message: "backend error" } },
        { type: "done", data: { turn_count: 3, total_tokens: 12 } },
      ];
      const wire = events
        .map(
          (event, i) =>
            `data:${JSON.stringify(event)}${i === 0 ? "\r\n\r\n" : i === events.length - 1 ? "" : "\n\n"}`,
        )
        .join("");
      const body = new ReadableStream({
        start(controller) {
          for (const byte of new TextEncoder().encode(wire))
            controller.enqueue(Uint8Array.of(byte));
          controller.close();
        },
      });
      const frames = [];
      const states = [];
      let state = caller.initialTurnState();
      await invoke({
        fetchImpl: async () => ({ ok: true, body }),
        onFrame: (value) => {
          frames.push(value);
          state = caller.applyFrame(state, value);
          states.push(state);
          // Existing callers ignored dispatch return values, including false.
          return false;
        },
      });
      expect(frames).toEqual(events);
      expect(state.assistant).toBe("雪🙂");
      expect(state.status).toBe("done");
      expect(states[3].error).toBe("backend error");
      if (name === "grimoire") {
        expect(states[1].status).toBe("busy");
        expect(state.touched.map((node) => node.id)).toEqual([1]);
        expect(state.turnCount).toBe(3);
        expect(state.totalTokens).toBe(12);
      }
      expect(body.locked).toBe(false);
    });

    it("preserves its missing-body behavior", async () => {
      const onFrame = vi.fn();
      await invoke({
        fetchImpl: async () => ({ ok: true, body: null }),
        onFrame,
      });
      if (name === "moving")
        expect(onFrame).toHaveBeenCalledWith({
          type: "error",
          data: { message: "Something went wrong. Please try again." },
        });
      else expect(onFrame).not.toHaveBeenCalled();
    });

    it("propagates callback failure and unlocks", async () => {
      const failure = new Error("dispatch");
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(
            new TextEncoder().encode('data: {"type":"done"}\n\n'),
          );
        },
      });
      await expect(
        invoke({
          fetchImpl: async () => ({ ok: true, body }),
          onFrame: () => {
            throw failure;
          },
        }),
      ).rejects.toBe(failure);
      expect(body.locked).toBe(false);
    });

    it("passes abort through fetch and cancels a pending body read", async () => {
      const abort = new AbortController();
      const reason = new Error("stop");
      const cancel = vi.fn();
      const body = new ReadableStream({ cancel });
      const fetchImpl = vi.fn(async () => ({ ok: true, body }));
      const onFrame = vi.fn();
      const outcome = invoke({
        fetchImpl,
        signal: abort.signal,
        onFrame,
      }).catch((error) => error);
      await vi.waitFor(() => expect(body.locked).toBe(true));
      abort.abort(reason);
      expect(await outcome).toBe(reason);
      expect(fetchImpl.mock.calls[0][1].signal).toBe(abort.signal);
      expect(cancel).toHaveBeenCalledOnce();
      expect(body.locked).toBe(false);
      expect(onFrame).not.toHaveBeenCalled();
    });
  });
}
