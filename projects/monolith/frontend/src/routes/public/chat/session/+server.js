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
  // Turnstile token forwarded by the client widget. The FastAPI backend runs
  // siteverify (the Turnstile secret lives there, never in SSR); no valid
  // token, no session (the backend returns 403 turnstile_failed).
  let turnstileToken = null;
  try {
    const body = await request.json();
    if (body && typeof body.turnstile_token === "string") {
      turnstileToken = body.turnstile_token;
    }
  } catch {
    // A missing or empty body falls through: the backend treats an absent token
    // as a failed challenge in production (it only stub-accepts when its secret
    // is unset, i.e. dev/test).
  }

  // Forward coarse geo + user-agent for the backend's pseudonymous session row,
  // plus the real client IP from Cloudflare's CF-Connecting-IP header so the
  // backend can salt-and-hash it (ip_hash, for reactive abuse forensics). The
  // backend trusts CF-Connecting-IP only on connections from the frontend (the
  // web CiliumNetworkPolicy admits only the frontend pod), so a direct caller
  // cannot spoof it.
  const headers = { "content-type": "application/json" };
  const country = request.headers.get("cf-ipcountry");
  if (country) headers["CF-IPCountry"] = country;
  const userAgent = request.headers.get("user-agent");
  if (userAgent) headers["User-Agent"] = userAgent;
  const connectingIp = request.headers.get("cf-connecting-ip");
  if (connectingIp) headers["CF-Connecting-IP"] = connectingIp;

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
