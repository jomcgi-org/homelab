const API_BASE = process.env.API_BASE;

export async function GET({ params }) {
  try {
    const res = await fetch(
      `${API_BASE}/api/agents/sessions/${encodeURIComponent(params.id)}`,
      { signal: AbortSignal.timeout(10000) },
    );
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "session unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}

export async function DELETE({ params }) {
  try {
    const res = await fetch(
      `${API_BASE}/api/agents/sessions/${encodeURIComponent(params.id)}`,
      { method: "DELETE", signal: AbortSignal.timeout(10000) },
    );
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "session delete failed" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
