import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "./+server.js";
import { MAX_MAX_TOKENS, upstreamBody } from "./body.js";

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.QWEN_API_BASE;
});

const request = (body) => ({
  json: async () => body,
  signal: new AbortController().signal,
});

describe("upstreamBody", () => {
  it("forwards only chat messages and pins model, streaming and usage", () => {
    const body = upstreamBody({
      messages: [
        { role: "user", content: "hi", extra: 1 },
        { role: "tool", content: "x" },
        { role: "assistant", content: 5 },
      ],
      model: "other",
      temperature: 0,
      maxTokens: 64,
      enableThinking: false,
    });
    expect(body).toEqual({
      model: "qwen3.6-27b",
      messages: [{ role: "user", content: "hi" }],
      stream: true,
      stream_options: { include_usage: true },
      max_tokens: 64,
      chat_template_kwargs: { enable_thinking: false },
    });
  });

  it("clamps the token budget and defaults thinking on", () => {
    const body = upstreamBody({
      messages: [{ role: "user", content: "hi" }],
      maxTokens: MAX_MAX_TOKENS + 1,
    });
    expect(body.max_tokens).toBe(8192);
    expect(body.chat_template_kwargs.enable_thinking).toBe(true);
  });
});

describe("/private/qwen/chat POST", () => {
  it("answers 503 without QWEN_API_BASE", async () => {
    const res = await POST({ request: request({ messages: [] }) });
    expect(res.status).toBe(503);
  });

  it("streams from the configured FreeToken endpoint", async () => {
    process.env.QWEN_API_BASE = "http://qwen.test:8090/";
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: null });
    vi.stubGlobal("fetch", fetchMock);
    const res = await POST({
      request: request({ messages: [{ role: "user", content: "hi" }] }),
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://qwen.test:8090/v1/chat/completions",
    );
    expect(res.headers.get("Content-Type")).toBe("text/event-stream");
  });

  it("rejects an empty conversation", async () => {
    process.env.QWEN_API_BASE = "http://qwen.test:8090";
    const res = await POST({ request: request({ messages: [] }) });
    expect(res.status).toBe(400);
  });
});
