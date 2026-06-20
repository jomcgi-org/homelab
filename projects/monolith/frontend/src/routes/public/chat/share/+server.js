import { json } from "@sveltejs/kit";

// Server-only BFF for "share this chat" (ADR 005 follow-up). The browser POSTs
// here same-origin (jomcgi.dev/chat/share, rerouted to /public/chat/share); the
// session id comes from the httpOnly cookie set by /chat/session, never from the
// body. The backend mints the snapshot SERVER-SIDE from the stored transcript
// (no client content is forwarded), so a forged body cannot put words in the
// model's mouth in a public artifact. The browser never talks to the internal
// chat API directly.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

const SESSION_COOKIE = "cps";

export async function POST({ cookies }) {
  const sessionId = cookies.get(SESSION_COOKIE);
  if (!sessionId) {
    // No session cookie: identical 404 to the backend's missing/expired case.
    return json({ detail: "Session not found" }, { status: 404 });
  }

  // Only the session id (from the cookie) is forwarded. No transcript content
  // is read from the request body: the snapshot is minted from the backend's
  // server-authoritative record.
  const resp = await fetch(`${CHAT_API_BASE}/internal/chat/share`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
    signal: AbortSignal.timeout(10_000),
  });

  if (!resp.ok) {
    // Backend errors are JSON: 404 (invalid/expired session), 400 (nothing to
    // share). Relay status + body so the client can react.
    let detail;
    try {
      detail = await resp.json();
    } catch {
      detail = { detail: "share unavailable" };
    }
    return json(detail, { status: resp.status });
  }

  // { snapshot_id } -> the client builds the absolute share URL from it.
  const body = await resp.json();
  return json({ snapshot_id: body.snapshot_id });
}
