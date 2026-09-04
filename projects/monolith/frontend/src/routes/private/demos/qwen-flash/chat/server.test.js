import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "./+server.js";

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.QWEN_FLASH_API_BASE;
});

describe("/private/demos/qwen-flash/chat POST", () => {
  it("enables thinking and pins the output limit in every upstream request", async () => {
    process.env.QWEN_FLASH_API_BASE = "http://qwen.test:8000";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      body: null,
    });
    vi.stubGlobal("fetch", fetchMock);
    const request = {
      json: async () => ({
        messages: [{ role: "user", content: "Think carefully" }],
        max_tokens: 12,
        chat_template_kwargs: { enable_thinking: false },
      }),
      signal: new AbortController().signal,
    };

    await POST({ request });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://qwen.test:8000/v1/chat/completions");
    expect(JSON.parse(init.body)).toEqual({
      messages: [{ role: "user", content: "Think carefully" }],
      model: "qwen3.6-27b",
      stream: true,
      max_tokens: 8192,
      chat_template_kwargs: { enable_thinking: true },
    });
  });
});
