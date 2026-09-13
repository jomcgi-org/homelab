const API_BASE = process.env.API_BASE;

export async function GET({ url }) {
  const q = url.searchParams.get("q")?.trim();
  const limit = url.searchParams.get("limit") || "20";
  if (!q) {
    return new Response(JSON.stringify({ error: "q is required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const upstream = new URL(`${API_BASE}/api/agents/search`);
    upstream.searchParams.set("q", q);
    upstream.searchParams.set("limit", limit);
    const res = await fetch(upstream, { signal: AbortSignal.timeout(10000) });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "search unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
