import { json } from "@sveltejs/kit";

// Server-only BFF for public chat session creation (ADR 005, Phase 1). The
// browser POSTs here same-origin (jomcgi.dev/chat/session, rerouted to
// /public/chat/session); this handler is the only thing that talks to the
// internal chat API. The chat API is NOT on the public HTTPRoute, so the
// browser never reaches it directly.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

// Opaque, httpOnly session cookie. The value is the backend's opaque session
// id; the cookie is its only carrier (we never return the id in the body). The
// row, not the cookie, is the authority for every later budget.
const SESSION_COOKIE = "cps";
// Mirrors CHAT_PUBLIC_SESSION_TTL_SECONDS (chart values, default 1800s).
const SESSION_COOKIE_MAX_AGE = 1800;

export async function POST({ request, cookies }) {
  // Optional Turnstile token forwarded by the client. Phase 1 stub-accepts any
  // or none; real siteverify (in the FastAPI backend) lands in Phase 2.
  let turnstileToken = null;
  try {
    const body = await request.json();
    if (body && typeof body.turnstile_token === "string") {
      turnstileToken = body.turnstile_token;
    }
  } catch {
    // A missing or empty body is fine in Phase 1.
  }

  // Forward coarse geo + user-agent for the backend's pseudonymous session row.
  // TODO(Phase 2): also forward the Cloudflare CF-Connecting-IP so the backend
  // can key its per-IP session-mint limiter. The backend trusts that header
  // only on connections bearing SSR's Linkerd identity.
  const headers = { "content-type": "application/json" };
  const country = request.headers.get("cf-ipcountry");
  if (country) headers["CF-IPCountry"] = country;
  const userAgent = request.headers.get("user-agent");
  if (userAgent) headers["User-Agent"] = userAgent;

  const resp = await fetch(`${CHAT_API_BASE}/internal/chat/session`, {
    method: "POST",
    headers,
    body: JSON.stringify(
      turnstileToken ? { turnstile_token: turnstileToken } : {},
    ),
    signal: AbortSignal.timeout(10_000),
  });

  if (!resp.ok) {
    return json({ ok: false }, { status: resp.status });
  }

  const { session_id: sessionId } = await resp.json();
  cookies.set(SESSION_COOKIE, sessionId, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_COOKIE_MAX_AGE,
  });

  // The session id stays server-side; the cookie is the carrier.
  return json({ ok: true });
}
