const API_BASE = process.env.API_BASE;

export async function POST({ request }) {
  try {
    let body = {};
    try {
      body = await request.json();
    } catch {
      body = {};
    }
    const email = request.headers.get("x-auth-email");
    const payload = {};
    if (body?.companion_id) payload.companion_id = body.companion_id;
    const res = await fetch(`${API_BASE}/api/agents/companion`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(email ? { "X-Auth-Email": email } : {}),
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(
      JSON.stringify({ error: "companion register failed" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      },
    );
  }
}
