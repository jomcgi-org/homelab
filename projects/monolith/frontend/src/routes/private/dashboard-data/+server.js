const API_BASE = process.env.API_BASE || "http://localhost:8000";

/**
 * GET proxy for the backend dashboard aggregate. The dashboard page's
 * 60s client refresh hits this same-origin endpoint instead of the
 * cluster-internal API base.
 */
export async function GET() {
  try {
    const res = await fetch(`${API_BASE}/api/home/dashboard`, {
      signal: AbortSignal.timeout(15000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "dashboard unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
