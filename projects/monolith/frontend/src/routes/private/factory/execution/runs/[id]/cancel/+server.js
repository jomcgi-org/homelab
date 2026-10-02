const API_BASE = process.env.API_BASE;

export async function POST({ params, request }) {
  const email = request.headers.get("x-auth-email");
  try {
    const res = await fetch(
      `${API_BASE}/api/swarm/runs/${encodeURIComponent(params.id)}/cancel`,
      {
        method: "POST",
        headers: {
          "Content-Type":
            request.headers.get("content-type") || "application/json",
          // The verified identity is the claim Envoy projected into
          // X-Auth-Email, which the gateway strips on ingress. Nothing
          // validates Cf-Access-Authenticated-User-Email, so it is never
          // forwarded as an identity (#6036).
          ...(email ? { "X-Auth-Email": email } : {}),
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
