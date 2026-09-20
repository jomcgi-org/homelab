const API_BASE = process.env.API_BASE;

export async function GET({ params, request }) {
  const email = request.headers.get("x-auth-email");
  try {
    const res = await fetch(
      `${API_BASE}/api/agents/factory/escalations/${encodeURIComponent(params.id)}/context`,
      {
        headers: email ? { "X-Auth-Email": email } : {},
        signal: AbortSignal.timeout(15000),
      },
    );
    return new Response(await res.text(), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "context unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
