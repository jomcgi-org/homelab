// Write proxy for one escalation decision. The Cloudflare Access email is the
// only identity the browser has, and the backend gate reads it from this
// header, so it is forwarded verbatim and nothing else is added: a request
// that arrives here without it is one the backend must refuse, not one this
// proxy should paper over.
const API_BASE = process.env.API_BASE;

export async function POST({ params, request }) {
  const email = request.headers.get("Cf-Access-Authenticated-User-Email");
  try {
    const res = await fetch(
      `${API_BASE}/api/agents/factory/decisions/${encodeURIComponent(params.id)}`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(email ? { "Cf-Access-Authenticated-User-Email": email } : {}),
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
