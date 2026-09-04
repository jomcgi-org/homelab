import { qwenFlashApiBase } from "../upstream.js";

const MODEL = "qwen3.6-27b";
const REQUEST_TIMEOUT_MS = 300000;

// 400 keeps a reasoned answer near 20 s at the roughly 20 tok/s this server
// sustains with thinking on. 600 pushes it past 30 s, which is too long to hold
// a room during a live demo.
const DEFAULT_MAX_TOKENS = 400;
const MAX_MAX_TOKENS = 2048;

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
      temperature: 0,
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
