// Server-side proxy for the drain console read. The browser never reaches
// the FastAPI backend directly (see hooks.server.js), so this mirrors
// drain-lane/+server.js: fetch against API_BASE and relay the JSON.
const API_BASE = process.env.API_BASE;

export async function GET() {
  try {
    const response = await fetch(`${API_BASE}/api/agents/drain/console`, {
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      throw new Error(`backend ${response.status}`);
    }
    return new Response(JSON.stringify(await response.json()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(
      JSON.stringify({ error: "drain console unavailable" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      },
    );
  }
}
