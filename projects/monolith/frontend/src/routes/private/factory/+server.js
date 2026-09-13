// Server-side proxy for the factory board. The browser never reaches the
// FastAPI backend directly (see hooks.server.js), so this mirrors
// drain-lane/+server.js: fetch against API_BASE and relay the JSON. The
// optional task query asks the backend to load one finished task's plan.
const API_BASE = process.env.API_BASE;

export async function GET({ url }) {
  try {
    const task = url.searchParams.get("task");
    const query = task ? `?task=${encodeURIComponent(task)}` : "";
    const response = await fetch(`${API_BASE}/api/agents/factory${query}`, {
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      throw new Error(`backend ${response.status}`);
    }
    return new Response(JSON.stringify(await response.json()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "factory unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
