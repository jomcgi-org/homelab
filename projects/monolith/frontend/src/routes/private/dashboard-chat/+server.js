const API_BASE = process.env.API_BASE || "http://localhost:8000";

/**
 * SSE proxy for the cluster chat agent. Mirrors routes/private/chat/+server.js
 * (the knowledge explorer proxy) but targets /api/chat/cluster.
 */
export async function POST({ request }) {
  const body = await request.json();

  const upstream = await fetch(`${API_BASE}/api/chat/cluster`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(120000),
  });

  if (!upstream.ok) {
    return new Response(JSON.stringify({ error: "upstream failed" }), {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  }

  return new Response(upstream.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}
