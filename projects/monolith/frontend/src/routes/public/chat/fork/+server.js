import { json } from "@sveltejs/kit";

// Server-only BFF for "fork this chat" (ADR 005 follow-up). A shared snapshot is
// read-only; forking it mints a NEW server-side session seeded with the
// snapshot's frozen transcript so the visitor can continue it. The browser POSTs
// here same-origin (jomcgi.dev/chat/fork, rerouted to /public/chat/fork) with a
// solved Turnstile token + the snapshot id; this handler forwards them to the
// internal chat API, which verifies the challenge (the Turnstile secret lives in
// the backend, never SSR) and seeds the new session SERVER-SIDE from the stored
// snapshot. On success the opaque session id lands in the httpOnly cookie, never
// the body, exactly like /chat/session. The browser never talks to the internal
// chat API directly.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

// Opaque, httpOnly session cookie (same carrier as /chat/session): the value is
// the backend's opaque session id; the row, not the cookie, is the budget
// authority.
const SESSION_COOKIE = "cps";
// Mirrors CHAT_PUBLIC_SESSION_TTL_SECONDS (chart values, default 1800s).
const SESSION_COOKIE_MAX_AGE = 1800;

export async function POST({ request, cookies }) {
  // The snapshot id to fork and the solved Turnstile token. A missing/invalid
  // body falls through with nulls: the backend rejects an absent token as a
  // failed challenge (in production) and an unknown snapshot as a 404.
  let snapshotId = null;
  let turnstileToken = null;
  try {
    const body = await request.json();
    if (body && typeof body.snapshot_id === "string") {
      snapshotId = body.snapshot_id;
    }
    if (body && typeof body.turnstile_token === "string") {
      turnstileToken = body.turnstile_token;
    }
  } catch {
    // Empty/malformed body: forwarded as nulls; the backend fails the challenge.
  }

  // Forward coarse geo + user-agent for the new session's pseudonymous row, plus
  // the real client IP from Cloudflare's CF-Connecting-IP header (salted-hashed
  // by the backend for reactive abuse forensics). The backend trusts
  // CF-Connecting-IP only on connections bearing SSR's Linkerd identity, so a
  // direct caller cannot spoof it.
  const headers = { "content-type": "application/json" };
  const country = request.headers.get("cf-ipcountry");
  if (country) headers["CF-IPCountry"] = country;
  const userAgent = request.headers.get("user-agent");
  if (userAgent) headers["User-Agent"] = userAgent;
  const connectingIp = request.headers.get("cf-connecting-ip");
  if (connectingIp) headers["CF-Connecting-IP"] = connectingIp;

  const resp = await fetch(`${CHAT_API_BASE}/internal/chat/fork`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      snapshot_id: snapshotId,
      turnstile_token: turnstileToken,
    }),
    signal: AbortSignal.timeout(10_000),
  });

  if (!resp.ok) {
    // Backend errors: 403 (failed challenge), 404 (unknown snapshot). Relay the
    // status; no session cookie is set.
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

  // The session id stays server-side; the cookie is the carrier. The client now
  // navigates to the live app, which rehydrates the seeded transcript.
  return json({ ok: true });
}
