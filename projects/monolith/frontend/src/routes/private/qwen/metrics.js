// Stream parsing and inference stats for the private Qwen chat page.
//
// Timings are taken in the browser, so TTFT includes the proxy and tailnet hop to
// node-4; the server's own view of each request comes from /v1/requests.

// Split an SSE byte stream into complete `data:` payloads. Returns the parsed
// events and the unconsumed tail to prepend to the next read.
export function parseSseChunk(buffer) {
  const events = [];
  const blocks = buffer.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;
    if (data === "[DONE]") {
      events.push({ done: true });
      continue;
    }
    try {
      events.push({ json: JSON.parse(data) });
    } catch {
      // A malformed frame is dropped rather than ending the stream.
    }
  }
  return { events, rest };
}

// Fold one parsed chunk into a turn accumulator. `now` is a millisecond clock.
export function applyChunk(turn, json, now) {
  const choice = json?.choices?.[0];
  const delta = choice?.delta ?? {};
  const reasoning = delta.reasoning_content ?? "";
  const content = delta.content ?? "";
  if ((reasoning || content) && turn.firstTokenAt == null) {
    turn.firstTokenAt = now;
  }
  if (reasoning) turn.reasoning += reasoning;
  if (content) {
    if (turn.answerAt == null) turn.answerAt = now;
    turn.content += content;
  }
  if (choice?.finish_reason) turn.finishReason = choice.finish_reason;
  if (json?.usage) turn.usage = json.usage;
  return turn;
}

export function newTurn(sentAt) {
  return {
    sentAt,
    firstTokenAt: null,
    answerAt: null,
    endedAt: null,
    reasoning: "",
    content: "",
    finishReason: null,
    usage: null,
    error: null,
  };
}

// Per-turn stats from the accumulator and the final usage chunk.
export function turnStats(turn) {
  const usage = turn.usage ?? {};
  const prompt = usage.prompt_tokens ?? null;
  const completion = usage.completion_tokens ?? null;
  const cached = usage.prompt_tokens_details?.cached_tokens ?? 0;
  const ttftMs =
    turn.firstTokenAt != null ? turn.firstTokenAt - turn.sentAt : null;
  const decodeMs =
    turn.endedAt != null && turn.firstTokenAt != null
      ? turn.endedAt - turn.firstTokenAt
      : null;
  const newPrompt = prompt != null ? Math.max(0, prompt - cached) : null;
  return {
    promptTokens: prompt,
    completionTokens: completion,
    cachedTokens: cached,
    cacheHitRate: prompt ? cached / prompt : null,
    ttftMs,
    // Tokens actually prefilled (not served from the prefix cache) per second of TTFT.
    prefillTps:
      newPrompt != null && ttftMs ? (newPrompt * 1000) / ttftMs : null,
    // The first token lands at TTFT, so the rest were decoded in decodeMs.
    decodeTps:
      completion != null && completion > 1 && decodeMs
        ? ((completion - 1) * 1000) / decodeMs
        : null,
    thinkingMs:
      turn.answerAt != null && turn.firstTokenAt != null
        ? turn.answerAt - turn.firstTokenAt
        : null,
    wallMs: turn.endedAt != null ? turn.endedAt - turn.sentAt : null,
    contextTokens:
      prompt != null && completion != null ? prompt + completion : null,
  };
}

function mean(values) {
  const kept = values.filter((v) => v != null && Number.isFinite(v));
  return kept.length ? kept.reduce((a, b) => a + b, 0) / kept.length : null;
}

// Session totals over completed turns.
export function sessionStats(turns) {
  const stats = turns.filter((t) => t.usage).map(turnStats);
  const sum = (key) => stats.reduce((acc, s) => acc + (s[key] ?? 0), 0);
  const prompt = sum("promptTokens");
  const cached = sum("cachedTokens");
  return {
    turns: stats.length,
    promptTokens: prompt,
    completionTokens: sum("completionTokens"),
    cachedTokens: cached,
    cacheHitRate: prompt ? cached / prompt : null,
    meanTtftMs: mean(stats.map((s) => s.ttftMs)),
    meanDecodeTps: mean(stats.map((s) => s.decodeTps)),
    generationMs: sum("wallMs"),
    contextTokens: stats.length ? stats[stats.length - 1].contextTokens : 0,
  };
}

export function formatMs(ms) {
  if (ms == null) return "–";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export function formatRate(value) {
  return value == null ? "–" : `${value.toFixed(1)} tok/s`;
}

export function formatCount(value) {
  return value == null ? "–" : value.toLocaleString("en-US");
}

export function formatBytes(bytes) {
  if (bytes == null) return "–";
  return `${(bytes / 2 ** 30).toFixed(1)} GiB`;
}
