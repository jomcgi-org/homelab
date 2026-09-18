const API_BASE = process.env.API_BASE;

export async function POST({ params, request }) {
  const email = request.headers.get("x-auth-email");
  try {
    const res = await fetch(
      `${API_BASE}/api/agents/sessions/${encodeURIComponent(params.id)}/stop`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(email ? { "X-Auth-Email": email } : {}),
        },
        body: await request.text(),
        signal: AbortSignal.timeout(35000),
      },
    );
    return new Response(await res.text(), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(
      JSON.stringify({
        status: "unknown",
        outcome: "cessation_unconfirmed",
      }),
      { status: 502, headers: { "Content-Type": "application/json" } },
    );
  }
}
