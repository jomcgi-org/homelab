const API_BASE = process.env.API_BASE;

export async function POST({ url }) {
  try {
    const upstream = new URL(`${API_BASE}/api/agents/codex-login/start`);
    const grant = url.searchParams.get("grant");
    if (grant) upstream.searchParams.set("grant", grant);
    const res = await fetch(upstream, {
      method: "POST",
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(
      JSON.stringify({ error: "Codex login start unavailable" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      },
    );
  }
}
