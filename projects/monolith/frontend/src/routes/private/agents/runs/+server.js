const API_BASE = process.env.API_BASE;

export async function GET({ url }) {
  try {
    const active = url.searchParams.get("active");
    const query =
      active === null ? "" : `?active=${encodeURIComponent(active)}`;
    const res = await fetch(`${API_BASE}/api/swarm/runs${query}`, {
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "swarm runs unavailable" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}
