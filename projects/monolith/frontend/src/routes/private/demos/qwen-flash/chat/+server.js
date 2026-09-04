import { qwenFlashApiBase } from "../upstream.js";

const MODEL = "qwen3.6-27b";
const REQUEST_TIMEOUT_MS = 300000;

// Thinking and the visible answer share this budget. A short cap can end the
// reasoning trace before the audience sees an answer.
const DEFAULT_MAX_TOKENS = 8192;
const MAX_MAX_TOKENS = 8192;

export async function POST({ request }) {
  const body = await request.json();
  // The client owns both of these so the demo can toggle thinking mid-session
  // and show the contrast. Read them explicitly rather than spreading the
  // request body upstream, so only fields we intend to expose are forwarded.
  const enableThinking = body.enableThinking !== false;
  const maxTokens =
    Number.isInteger(body.maxTokens) &&
    body.maxTokens > 0 &&
    body.maxTokens <= MAX_MAX_TOKENS
      ? body.maxTokens
      : DEFAULT_MAX_TOKENS;
  const signal = AbortSignal.any([
    request.signal,
    AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  ]);
  const upstream = await fetch(`${qwenFlashApiBase()}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messages: body.messages,
      model: MODEL,
      stream: true,
      // No temperature here on purpose: the server resolves sampling from the
      // model's own generation_config, which is tuned for this model's thinking
      // mode. Forcing greedy decoding would override that.
      max_tokens: maxTokens,
      chat_template_kwargs: { enable_thinking: enableThinking },
    }),
    signal,
  });

  if (!upstream.ok) {
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  }

  return new Response(upstream.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}
