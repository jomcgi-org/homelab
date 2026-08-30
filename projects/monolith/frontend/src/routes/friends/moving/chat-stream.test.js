import { describe, expect, it, vi } from "vitest";
import {
  MOVING_CHAT_PATH,
  applyFrame,
  createSseParser,
  initialTurnState,
  streamChatMessage,
} from "./chat-stream.js";

function frame(type, data) {
  return `data: ${JSON.stringify({ type, data })}\n\n`;
}

function streamFrom(chunks) {
  const encoder = new TextEncoder();
  const queue = [...chunks];
  return new ReadableStream({
    pull(controller) {
      if (queue.length === 0) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(queue.shift()));
    },
  });
}

describe("moving chat SSE parser", () => {
  it("parses complete and split frames", () => {
    const parser = createSseParser();
    const whole = frame("token", { text: "hello" });
    const middle = Math.floor(whole.length / 2);
    expect(parser.push(whole.slice(0, middle))).toEqual([]);
    expect(parser.push(whole.slice(middle))).toEqual([
      { type: "token", data: { text: "hello" } },
    ]);
  });

  it("tolerates CRLF and flushes a trailing frame", () => {
    const parser = createSseParser();
    expect(parser.push('data: {"type":"done","data":{}}\r\n\r\n')).toEqual([
      { type: "done", data: {} },
    ]);
    expect(parser.push(frame("token", { text: "x" }).trimEnd())).toEqual([]);
    expect(parser.flush()).toEqual([{ type: "token", data: { text: "x" } }]);
  });
});

describe("moving chat reducer", () => {
  it("accumulates token text and completes", () => {
    let state = initialTurnState();
    state = applyFrame(state, { type: "token", data: { text: "Pack " } });
    state = applyFrame(state, { type: "token", data: { text: "boxes" } });
    state = applyFrame(state, { type: "done", data: {} });
    expect(state).toEqual({
      status: "done",
      assistant: "Pack boxes",
      error: "",
    });
  });
});

describe("streamChatMessage", () => {
  it("posts history and emits streamed frames", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      body: streamFrom([
        frame("token", { text: "Pack " }),
        frame("token", { text: "boxes" }) + frame("done", {}),
      ]),
    });
    const frames = [];
    const history = [{ role: "user", content: "Earlier" }];

    await streamChatMessage("What is next?", history, {
      fetchImpl,
      onFrame: (item) => frames.push(item),
    });

    expect(fetchImpl).toHaveBeenCalledWith(
      MOVING_CHAT_PATH,
      expect.objectContaining({ method: "POST" }),
    );
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({
      message: "What is next?",
      history,
    });
    expect(frames.map((item) => item.type)).toEqual(["token", "token", "done"]);
  });

  it("maps an HTTP failure to an error frame", async () => {
    const frames = [];
    await streamChatMessage("Question", [], {
      fetchImpl: vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({ detail: "Unavailable" }),
      }),
      onFrame: (item) => frames.push(item),
    });
    expect(frames).toEqual([
      { type: "error", data: { code: "error", message: "Unavailable" } },
    ]);
  });
});
