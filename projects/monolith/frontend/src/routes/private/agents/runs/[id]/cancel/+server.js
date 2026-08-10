const API_BASE = process.env.API_BASE;

export async function POST({ params, request }) {
  try {
    const res = await fetch(
      `${API_BASE}/api/swarm/runs/${encodeURIComponent(params.id)}/cancel`,
      {
        method: "POST",
        headers: {
          "Content-Type":
            request.headers.get("content-type") || "application/json",
          ...(request.headers.get("Cf-Access-Authenticated-User-Email")
            ? {
                "Cf-Access-Authenticated-User-Email": request.headers.get(
                  "Cf-Access-Authenticated-User-Email",
                ),
              }
            : {}),
        },
        body: await request.text(),
        signal: AbortSignal.timeout(10000),
      },
    );
    return new Response(res.body, {
      status: res.status,
      headers: {
        "Content-Type": res.headers.get("content-type") || "application/json",
      },
    });
  } catch {
    return new Response(JSON.stringify({ error: "swarm run unavailable" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}
