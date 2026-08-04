const API_BASE = process.env.API_BASE;

export async function POST({ params, request }) {
  try {
    const body = await request.json();
    const res = await fetch(
      `${API_BASE}/api/agents/sessions/${encodeURIComponent(params.id)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: body.prompt,
          ...(body.model ? { model: body.model } : {}),
        }),
        signal: AbortSignal.timeout(10000),
      },
    );
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "message send failed" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
