const API_BASE = process.env.API_BASE;

export async function GET({ params, url }) {
  try {
    const since = url.searchParams.get("since") ?? "0";
    const upstream = new URL(
      `${API_BASE}/api/agents/companion/${encodeURIComponent(params.companionId)}/ledger`,
    );
    upstream.searchParams.set("since", since);
    const res = await fetch(upstream, { signal: AbortSignal.timeout(10000) });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "ledger unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
