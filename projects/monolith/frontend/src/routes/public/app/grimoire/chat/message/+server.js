import { json } from "@sveltejs/kit";

// Server-only BFF for a Grimoire chat turn (mirrors
// routes/public/chat/message/+server.js, the notes-chat proxy). The browser
// POSTs {message} here same-origin (/app/grimoire/chat/message); the session
// id comes from the httpOnly "gcs" cookie set by /app/grimoire/chat/session,
// never from the body. The upstream SSE response is passed straight back
// unbuffered. The browser never talks to the internal grimoire_chat API
// directly.
//
// Same backend, same env var as the notes chat surface (both surfaces are one
// public monolith binary): CHAT_API_BASE.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

// "gcs" (grimoire chat session), distinct from the notes chat's "cps" cookie
// so the two surfaces never collide on the same domain.
const SESSION_COOKIE = "gcs";

export async function POST({ request, cookies }) {
  const sessionId = cookies.get(SESSION_COOKIE);
  if (!sessionId) {
    // No session cookie: identical 404 to the backend's missing/expired case.
    return json({ detail: "Session not found" }, { status: 404 });
  }

  // Server is authoritative: the only client input honored is the single user
  // message string. Any client-supplied conversation history is ignored and
  // never forwarded; the backend's session row is the sole transcript.
  let message = "";
  try {
    const body = await request.json();
    if (body && typeof body.message === "string") {
      message = body.message;
    }
  } catch {
    message = "";
  }

  // No AbortSignal.timeout here: this is a streamed SSE response and a short
  // timeout would truncate the token stream (mirrors the notes-chat proxy).
  // nosemgrep: fetch-no-timeout
  const resp = await fetch(`${CHAT_API_BASE}/internal/grimoire-chat/message`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
  });

  if (!resp.ok) {
    // Backend errors are pre-stream JSON: 404 (invalid/expired session), 400
    // (char_cap), 429 (max_turns / max_session_tokens). Relay status + body.
    let detail;
    try {
      detail = await resp.json();
    } catch {
      detail = { detail: "chat unavailable" };
    }
    return json(detail, { status: resp.status });
  }

  // Pass the upstream SSE body stream straight through, unbuffered.
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
    },
  });
}
