import { json } from "@sveltejs/kit";

// Server-only BFF for "share this chat" (mirrors
// routes/public/chat/share/+server.js, the notes-chat proxy). The browser
// POSTs here same-origin (/app/grimoire/chat/share); the session id comes from
// the httpOnly "gcs" cookie set by /app/grimoire/chat/session, never from the
// body. The backend mints the snapshot SERVER-SIDE from the stored transcript
// (no client content is forwarded), so a forged body cannot put words in the
// model's mouth in a public artifact. The browser never talks to the internal
// grimoire_chat API directly.
//
// Same backend, same env var as the notes chat surface (both surfaces are one
// public monolith binary): CHAT_API_BASE.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

// "gcs" (grimoire chat session), distinct from the notes chat's "cps" cookie
// so the two surfaces never collide on the same domain.
const SESSION_COOKIE = "gcs";

export async function POST({ cookies }) {
  const sessionId = cookies.get(SESSION_COOKIE);
  if (!sessionId) {
    // No session cookie: identical 404 to the backend's missing/expired case.
    return json({ detail: "Session not found" }, { status: 404 });
  }

  // Only the session id (from the cookie) is forwarded. No transcript content
  // is read from the request body: the snapshot is minted from the backend's
  // server-authoritative record.
  const resp = await fetch(`${CHAT_API_BASE}/internal/grimoire-chat/share`, {
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
