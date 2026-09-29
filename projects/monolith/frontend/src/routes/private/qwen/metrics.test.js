import { describe, expect, it } from "vitest";
import {
  applyChunk,
  formatMs,
  newTurn,
  parseSseChunk,
  sessionStats,
  turnStats,
} from "./metrics.js";

const chunk = (delta, extra = {}) => ({
  choices: [{ delta, index: 0, finish_reason: null }],
  ...extra,
});

describe("parseSseChunk", () => {
  it("returns complete events and keeps the partial tail", () => {
    const { events, rest } = parseSseChunk(
      'data: {"a":1}\n\ndata: [DONE]\n\ndata: {"b"',
    );
    expect(events).toEqual([{ json: { a: 1 } }, { done: true }]);
    expect(rest).toBe('data: {"b"');
  });

  it("drops malformed frames and handles CRLF", () => {
    const { events } = parseSseChunk('data: {bad\r\n\r\ndata: {"c":2}\r\n\r\n');
    expect(events).toEqual([{ json: { c: 2 } }]);
  });
});

describe("turn stats", () => {
  it("measures TTFT, thinking, prefill and decode rates from the stream", () => {
    const turn = newTurn(1000);
    applyChunk(turn, chunk({ reasoning_content: "think" }), 1500);
    applyChunk(turn, chunk({ content: "Hi" }), 2500);
    applyChunk(
      turn,
      {
        choices: [],
        usage: {
          prompt_tokens: 1000,
          completion_tokens: 101,
          prompt_tokens_details: { cached_tokens: 800 },
        },
      },
      6400,
    );
    turn.endedAt = 6500;
    const stats = turnStats(turn);
    expect(turn.reasoning).toBe("think");
    expect(turn.content).toBe("Hi");
    expect(stats.ttftMs).toBe(500);
    expect(stats.thinkingMs).toBe(1000);
    expect(stats.cacheHitRate).toBeCloseTo(0.8);
    expect(stats.prefillTps).toBeCloseTo(400); // 200 new tokens in 0.5 s
    expect(stats.decodeTps).toBeCloseTo(20); // 100 tokens in 5 s
    expect(stats.contextTokens).toBe(1101);
    expect(stats.wallMs).toBe(5500);
  });

  it("leaves rates empty without usage", () => {
    const stats = turnStats(newTurn(0));
    expect(stats.decodeTps).toBeNull();
    expect(stats.prefillTps).toBeNull();
  });
});

describe("sessionStats", () => {
  it("sums tokens and averages rates over completed turns", () => {
    const done = (ttft, completion) => {
      const t = newTurn(0);
      t.firstTokenAt = ttft;
      t.endedAt = ttft + (completion - 1) * 50;
      t.usage = {
        prompt_tokens: 100,
        completion_tokens: completion,
        prompt_tokens_details: { cached_tokens: 50 },
      };
      return t;
    };
    const s = sessionStats([done(200, 11), done(400, 21), newTurn(0)]);
    expect(s.turns).toBe(2);
    expect(s.promptTokens).toBe(200);
    expect(s.completionTokens).toBe(32);
    expect(s.cacheHitRate).toBeCloseTo(0.5);
    expect(s.meanTtftMs).toBe(300);
    expect(s.meanDecodeTps).toBeCloseTo(20);
    expect(s.contextTokens).toBe(121);
  });
});

describe("formatMs", () => {
  it("switches to seconds at one second", () => {
    expect(formatMs(850)).toBe("850 ms");
    expect(formatMs(2345)).toBe("2.35 s");
    expect(formatMs(null)).toBe("–");
  });
});
