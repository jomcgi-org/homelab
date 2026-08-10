const API_BASE = process.env.API_BASE;

export async function GET({ params }) {
  try {
    const res = await fetch(
      `${API_BASE}/api/swarm/runs/${encodeURIComponent(params.id)}`,
      { signal: AbortSignal.timeout(10000) },
    );
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "swarm run unavailable" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}
