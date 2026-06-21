// The public Turnstile site key gates the chat (ADR 005). It is PUBLIC by design
// (it identifies the widget, not a credential); the Turnstile *secret* never
// enters the frontend, only the FastAPI backend. Read from the environment.
//
// The graph payload is NOT loaded here: the page defaults to the chat view and
// only fetches the graph (client-side, via /app/notes/graph) when the visitor
// switches to the graph view, so the initial chat load stays light.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

// The internal chat API base (server-side only; the browser never reaches it).
// Same seam the snapshot loader uses to read a shared transcript.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";
// Opaque httpOnly session cookie set by /chat/session and /chat/fork.
const SESSION_COOKIE = "cps";

// Rehydrate an existing session's transcript so a reload, or a freshly-forked
// session, lands the visitor straight back in their conversation instead of the
// admission gate. Returns { admitted, messages, tokens }: admitted only when the
// backend confirms a live session (a stale/expired cookie 404s and falls back to
// the gate). Fail-soft: any upstream hiccup leaves the visitor at the gate.
async function loadSession(fetch, cookies) {
  const sessionId = cookies?.get?.(SESSION_COOKIE);
  if (!sessionId) return { admitted: false, messages: [], tokens: 0 };
  try {
    const res = await fetch(`${CHAT_API_BASE}/internal/chat/transcript`, {
      headers: { "X-Chat-Session-Id": sessionId },
      signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) return { admitted: false, messages: [], tokens: 0 };
    const body = await res.json();
    return {
      admitted: true,
      messages: Array.isArray(body.messages) ? body.messages : [],
      tokens: typeof body.total_tokens === "number" ? body.total_tokens : 0,
    };
  } catch {
    return { admitted: false, messages: [], tokens: 0 };
  }
}

export async function load({ fetch, cookies }) {
  // Seed the live ticker's GPU readout with the cluster stats snapshot (the
  // same payload the homepage renders), via the same-origin proxy. The page
  // then polls it client-side. Fail-soft: the ticker falls back to its static
  // readouts when stats are unavailable, so a stats hiccup never blocks chat.
  let stats = null;
  try {
    const res = await fetch("/app/notes/stats");
    if (res.ok) stats = await res.json();
  } catch {
    // leave stats null; the ticker degrades gracefully
  }

  // Rehydrate an existing session (a reload, or a just-completed fork) so the
  // visitor resumes their conversation rather than re-passing the gate.
  const session = await loadSession(fetch, cookies);

  return {
    turnstileSiteKey: TURNSTILE_SITE_KEY,
    stats,
    admitted: session.admitted,
    initialMessages: session.messages,
    initialTokens: session.tokens,
  };
}
