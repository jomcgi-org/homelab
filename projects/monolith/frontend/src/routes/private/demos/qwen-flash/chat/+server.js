import { qwenFlashApiBase } from "../upstream.js";

const MODEL = "qwen3.6-27b";
const REQUEST_TIMEOUT_MS = 300000;

export async function POST({ request }) {
  const body = await request.json();
  const signal = AbortSignal.any([
    request.signal,
    AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  ]);
  const upstream = await fetch(`${qwenFlashApiBase()}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, model: MODEL, stream: true }),
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
