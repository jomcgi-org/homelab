import { json } from "@sveltejs/kit";

// Shared by the Grimoire chat session and fork BFFs. Same backend, same env
// var as the notes chat surface (both surfaces are one public monolith
// binary): CHAT_API_BASE.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

// "gcs" (grimoire chat session), distinct from the notes chat's "cps" cookie
// so the two surfaces never collide on the same domain. The value is the
// backend's opaque session id; the cookie is its only carrier (never the
// body). The row, not the cookie, is the authority for every later budget.
const SESSION_COOKIE = "gcs";
// Mirrors CHAT_PUBLIC_SESSION_TTL_SECONDS (chart values, default 1800s;
// grimoire_chat/limits.py shares the chat_public env vars deliberately).
const SESSION_COOKIE_MAX_AGE = 1800;

// POST `payload` to the internal grimoire_chat endpoint at `path`, which mints
// a session, and carry its id in the httpOnly cookie. Coarse geo, user-agent
// and Cloudflare's CF-Connecting-IP are forwarded for the pseudonymous session
// row (the IP is salted-hashed by the backend for reactive abuse forensics).
// The backend trusts CF-Connecting-IP only on connections from the frontend
// (enforced by the web CiliumNetworkPolicy), so a direct caller cannot spoof
// it. An upstream rejection relays its status and sets no cookie.
export async function mintSession({ request, cookies }, path, payload) {
  const headers = { "content-type": "application/json" };
  const country = request.headers.get("cf-ipcountry");
  if (country) headers["CF-IPCountry"] = country;
  const userAgent = request.headers.get("user-agent");
  if (userAgent) headers["User-Agent"] = userAgent;
  const connectingIp = request.headers.get("cf-connecting-ip");
  if (connectingIp) headers["CF-Connecting-IP"] = connectingIp;

  const resp = await fetch(`${CHAT_API_BASE}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
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
  return json({ ok: true });
}
