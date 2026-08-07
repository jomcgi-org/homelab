const API_BASE = process.env.API_BASE;

export async function GET() {
  try {
    const res = await fetch(`${API_BASE}/api/agents/vms`, {
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "vm state unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
