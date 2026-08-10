const API_BASE = process.env.API_BASE;

export async function GET({ url }) {
  try {
    const active = url.searchParams.get("active");
    const query =
      active === null ? "" : `?active=${encodeURIComponent(active)}`;
    const res = await fetch(`${API_BASE}/api/swarm/runs${query}`, {
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "swarm runs unavailable" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

export async function POST({ request }) {
  try {
    const body = await request.json();
    const email = request.headers.get("x-auth-email");
    const res = await fetch(`${API_BASE}/api/swarm/runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(email ? { "X-Auth-Email": email } : {}),
      },
      body: JSON.stringify({
        task: body.task,
        repo: body.repo,
        branch: body.branch,
        ...(body.idempotency_key
          ? { idempotency_key: body.idempotency_key }
          : {}),
        ...(body.budget_usd !== undefined && body.budget_usd !== ""
          ? { budget_usd: body.budget_usd }
          : {}),
      }),
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "swarm run create failed" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}
