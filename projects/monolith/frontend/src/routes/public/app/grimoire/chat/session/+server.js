import { mintSession } from "$lib/server/grimoire-chat-session.js";

// Server-only BFF for Grimoire chat session creation (began as a copy of the
// retired notes-chat proxy). The browser
// POSTs here same-origin (/app/grimoire/chat/session); this handler is the
// only thing that talks to the internal grimoire_chat API. That API is NOT on
// the public HTTPRoute, so the browser never reaches it directly.

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

  return mintSession(
    { request, cookies },
    "/internal/grimoire-chat/session",
    turnstileToken ? { turnstile_token: turnstileToken } : {},
  );
}
