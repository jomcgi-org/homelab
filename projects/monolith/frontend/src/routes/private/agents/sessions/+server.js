const API_BASE = process.env.API_BASE;

export async function POST({ request }) {
  try {
    const body = await request.json();
    const res = await fetch(`${API_BASE}/api/agents/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: body.prompt,
        ...(body.model ? { model: body.model } : {}),
        ...(body.workspace ? { workspace: body.workspace } : {}),
        ...(body.branch ? { branch: body.branch } : {}),
      }),
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "session create failed" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
