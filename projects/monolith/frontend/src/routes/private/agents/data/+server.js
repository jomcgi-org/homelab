const API_BASE = process.env.API_BASE;
const LOCAL_MODELS_ONLY = process.env.AGENTS_LOCAL_MODELS_ONLY === "true";

export async function GET() {
  try {
    const res = await fetch(`${API_BASE}/api/agents/sessions`, {
      signal: AbortSignal.timeout(10000),
    });
    const body = await res.json();
    return new Response(
      JSON.stringify({
        sessions: Array.isArray(body) ? body : (body.sessions ?? []),
        localModelsOnly: LOCAL_MODELS_ONLY,
      }),
      {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      },
    );
  } catch {
    return new Response(JSON.stringify({ error: "sessions unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
