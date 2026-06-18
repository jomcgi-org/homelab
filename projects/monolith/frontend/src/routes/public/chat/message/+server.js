import { json } from "@sveltejs/kit";

// Server-only BFF for a public chat turn (ADR 005, Phase 1). The browser POSTs
// {message} here same-origin (jomcgi.dev/chat/message, rerouted to
// /public/chat/message); the session id comes from the httpOnly cookie set by
// /chat/session, never from the body. The upstream SSE response is passed
// straight back unbuffered (mirrors /otel/v1/traces). The browser never talks
// to the internal chat API directly.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

const SESSION_COOKIE = "cps";

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
  // timeout would truncate the token stream (mirror the /otel passthrough,
  // which also passes resp.body straight through).
  const resp = await fetch(`${CHAT_API_BASE}/internal/chat/message`, {
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
