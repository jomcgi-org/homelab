// Server-side proxy for the escalation list. The browser never reaches the
// FastAPI backend directly (see hooks.server.js), so this relays the board and
// returns only the part this page renders.
const API_BASE = process.env.API_BASE;

export async function GET() {
  try {
    const response = await fetch(`${API_BASE}/api/agents/factory`, {
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      throw new Error(`backend ${response.status}`);
    }
    const board = await response.json();
    return new Response(
      JSON.stringify({ escalations: board.escalations ?? [] }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  } catch {
    return new Response(JSON.stringify({ error: "escalations unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
