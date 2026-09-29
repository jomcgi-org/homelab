import { notConfigured, qwenApiBase } from "../upstream.js";
import { upstreamBody } from "./body.js";

const REQUEST_TIMEOUT_MS = 600000;

export async function POST({ request }) {
  const base = qwenApiBase();
  if (!base) return notConfigured();
  const body = await request.json();
  const payload = upstreamBody(body);
  if (!payload.messages.length) {
    return new Response(JSON.stringify({ error: "no messages" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }
  const signal = AbortSignal.any([
    request.signal,
    AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  ]);
  let upstream;
  try {
    upstream = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: `FreeToken unreachable: ${err.message}` }),
      { status: 502, headers: { "Content-Type": "application/json" } },
    );
  }
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
