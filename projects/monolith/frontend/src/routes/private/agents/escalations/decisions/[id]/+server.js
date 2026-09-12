// Write proxy for one escalation decision. The verified identity is the email
// claim Envoy projected into X-Auth-Email from the Access JWT, which the
// gateway strips on ingress so it cannot be smuggled past the auth filter, and
// that is the header the backend gate reads. Forwarded the way
// ../../../sessions/+server.js and ../../../runs/+server.js forward it, and
// nothing else is added: a request that arrives with no verified identity is
// one the backend must refuse, not one this proxy should paper over.
const API_BASE = process.env.API_BASE;

export async function POST({ params, request }) {
  const email = request.headers.get("x-auth-email");
  try {
    const res = await fetch(
      `${API_BASE}/api/agents/factory/decisions/${encodeURIComponent(params.id)}`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(email ? { "X-Auth-Email": email } : {}),
        },
        body: await request.text(),
        signal: AbortSignal.timeout(30000),
      },
    );
    return new Response(await res.text(), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "decision unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
