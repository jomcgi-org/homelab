import { describe, it, expect, vi } from "vitest";
import {
  createSseParser,
  initialTurnState,
  applyFrame,
  streamChatMessage,
  MESSAGE_PROXY_PATH,
} from "./stream.js";

function f(type, data) {
  return `data: ${JSON.stringify({ type, data })}\n\n`;
}

function streamFrom(chunks) {
  const enc = new TextEncoder();
  const queue = Array.isArray(chunks) ? [...chunks] : [chunks];
  return new ReadableStream({
    pull(controller) {
      if (queue.length === 0) {
        controller.close();
        return;
      }
      controller.enqueue(enc.encode(queue.shift()));
    },
  });
}

describe("createSseParser", () => {
  it("emits one frame per blank-line-terminated block", () => {
    const p = createSseParser();
    const frames = p.push(
      f("token", { text: "a" }) + f("token", { text: "b" }),
    );
    expect(frames.map((x) => x.data.text)).toEqual(["a", "b"]);
  });

  it("reassembles a frame split across chunk boundaries", () => {
    const p = createSseParser();
    const whole = f("token", { text: "hello" });
    const mid = Math.floor(whole.length / 2);
    expect(p.push(whole.slice(0, mid))).toEqual([]);
    const frames = p.push(whole.slice(mid));
    expect(frames).toEqual([{ type: "token", data: { text: "hello" } }]);
  });

  it("ignores keep-alive / comment lines and tolerates CRLF", () => {
    const p = createSseParser();
    const frames = p.push(
      `: keep-alive\n\ndata: ${'{"type":"done","data":{}}'}\r\n\r\n`,
    );
    expect(frames).toEqual([{ type: "done", data: {} }]);
  });

  it("flush drains a trailing block with no final blank line", () => {
    const p = createSseParser();
    expect(p.push(f("token", { text: "x" }).trimEnd())).toEqual([]);
    expect(p.flush()).toEqual([{ type: "token", data: { text: "x" } }]);
  });
});

describe("applyFrame (turn-state reducer)", () => {
  it("accumulates streamed tokens and flips status to streaming", () => {
    let s = initialTurnState();
    s = applyFrame(s, { type: "token", data: { text: "Hel" } });
    s = applyFrame(s, { type: "token", data: { text: "lo" } });
    expect(s.assistant).toBe("Hello");
    expect(s.status).toBe("streaming");
  });

  it("collects node_touched into the grounding set, deduped and ordered", () => {
    let s = initialTurnState();
    s = applyFrame(s, {
      type: "node_touched",
      data: { id: 7, title: "Strahd von Zarovich" },
    });
    s = applyFrame(s, {
      type: "node_touched",
      data: { id: 3, title: "Counterspell" },
    });
    // Duplicate id is ignored (the backend can repeat across turns).
    s = applyFrame(s, {
      type: "node_touched",
      data: { id: 7, title: "Strahd von Zarovich" },
    });
    expect(s.touched).toEqual([
      { id: 7, title: "Strahd von Zarovich" },
      { id: 3, title: "Counterspell" },
    ]);
  });

  it("node_touched arrives before tokens and survives the token stream", () => {
    let s = initialTurnState();
    s = applyFrame(s, { type: "node_touched", data: { id: 1, title: "A" } });
    s = applyFrame(s, { type: "token", data: { text: "grounded" } });
    expect(s.touched).toEqual([{ id: 1, title: "A" }]);
    expect(s.assistant).toBe("grounded");
  });

  it("done records the turn/token counters", () => {
    let s = initialTurnState();
    s = applyFrame(s, {
      type: "done",
      data: { turn_count: 2, total_tokens: 44 },
    });
    expect(s.status).toBe("done");
    expect(s.turnCount).toBe(2);
    expect(s.totalTokens).toBe(44);
  });

  it("busy is a soft retryable state with the backend message", () => {
    let s = initialTurnState();
    s = applyFrame(s, {
      type: "busy",
      data: { code: "busy", message: "too busy" },
    });
    expect(s.status).toBe("busy");
    expect(s.error).toBe("too busy");
  });

  it("error carries a message and falls back when absent", () => {
    let s = applyFrame(initialTurnState(), { type: "error", data: {} });
    expect(s.status).toBe("error");
    expect(s.error).toBeTruthy();
  });

  it("ignores unknown frame types", () => {
    const s = initialTurnState();
    expect(applyFrame(s, { type: "whatever", data: {} })).toBe(s);
  });
});

describe("streamChatMessage", () => {
  it("POSTs the message to the SSR proxy and delivers parsed frames in order", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: streamFrom([
        f("node_touched", { id: 9, title: "Death House" }),
        f("token", { text: "hi" }),
        f("done", { turn_count: 1, total_tokens: 12 }),
      ]),
    });
    const frames = [];
    await streamChatMessage("hello", {
      fetchImpl,
      onFrame: (fr) => frames.push(fr),
    });

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(MESSAGE_PROXY_PATH);
    expect(init.method).toBe("POST");
    // Only the message string is sent; the session id rides the cookie.
    expect(JSON.parse(init.body)).toEqual({ message: "hello" });
    expect(frames.map((x) => x.type)).toEqual([
      "node_touched",
      "token",
      "done",
    ]);
  });

  it("reduces a full stream into transcript + grounding set via applyFrame", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: streamFrom([
        f("node_touched", { id: 1, title: "A" }) +
          f("node_touched", { id: 2, title: "B" }),
        f("token", { text: "two " }) + f("token", { text: "passages" }),
        f("done", { turn_count: 1, total_tokens: 5 }),
      ]),
    });
    let s = initialTurnState();
    await streamChatMessage("q", {
      fetchImpl,
      onFrame: (fr) => (s = applyFrame(s, fr)),
    });
    expect(s.assistant).toBe("two passages");
    expect(s.touched.map((n) => n.id)).toEqual([1, 2]);
    expect(s.status).toBe("done");
  });

  it("maps a pre-stream 429 to a single terminal error frame (no body read)", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      json: async () => ({ detail: { code: "max_turns", message: "limit" } }),
    });
    const frames = [];
    await streamChatMessage("x", {
      fetchImpl,
      onFrame: (fr) => frames.push(fr),
    });
    expect(frames).toHaveLength(1);
    expect(frames[0].type).toBe("error");
    expect(frames[0].data.code).toBe("max_turns");
    expect(frames[0].data.message).toBe(
      "This conversation has reached its length limit. Reload the page to start a new one.",
    );
  });

  it("maps a 404 with no body to a friendly session-expired error", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => {
        throw new Error("no body");
      },
    });
    const frames = [];
    await streamChatMessage("x", {
      fetchImpl,
      onFrame: (fr) => frames.push(fr),
    });
    expect(frames[0].type).toBe("error");
    expect(frames[0].data.message).toMatch(/expired/i);
  });
});
