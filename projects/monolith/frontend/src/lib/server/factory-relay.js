// Server-side relays for the private factory execution routes. The browser
// never reaches the FastAPI backend directly (see hooks.server.js).
const API_BASE = process.env.API_BASE;

// POST /api/swarm/runs/<segments> with the request body, passing the upstream
// status and body through. Each segment is URI-encoded.
export async function relayRunAction(request, segments) {
  const email = request.headers.get("x-auth-email");
  try {
    const path = segments.map((segment) => encodeURIComponent(segment));
    const res = await fetch(`${API_BASE}/api/swarm/runs/${path.join("/")}`, {
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
    });
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

// GET a backend JSON read and relay it, or answer 502 with `error`.
export async function relayJson(path, error) {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      throw new Error(`backend ${response.status}`);
    }
    return new Response(JSON.stringify(await response.json()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
