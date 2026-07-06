// Server-loads any existing Grimoire chat session so a reload, or a freshly
// forked session, lands the visitor back in their conversation instead of the
// admission gate. Mirrors routes/public/app/notes/+page.server.js's session
// rehydration; the ticker/stats seeding that file also does is intentionally
// dropped here (the Grimoire chat has no live ticker).
//
// This still runs as a server load even though the whole /app/grimoire route
// tree is ssr=false (see routes/public/app/grimoire/book/[book]/+page.server.js
// for why): a +page.server.js load always executes server-side and ships its
// result down as data, regardless of the ssr flag, which is what lets this
// read the httpOnly session cookie without ever exposing it to the browser.
// The localhost fallback is the established convention across every public
// proxy/loader (ships/stars/notes/campsites/grimoire); prod sets CHAT_API_BASE
// via values.yaml.
// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";
// "gcs" (grimoire chat session), distinct from the notes chat's "cps" cookie.
const SESSION_COOKIE = "gcs";

export async function load({ fetch, cookies }) {
  const sessionId = cookies?.get?.(SESSION_COOKIE);
  if (!sessionId) {
    return { admitted: false, initialMessages: [], initialTokens: 0 };
  }
  try {
    const res = await fetch(
      `${CHAT_API_BASE}/internal/grimoire-chat/transcript`,
      {
        headers: { "X-Chat-Session-Id": sessionId },
        signal: AbortSignal.timeout(5_000),
      },
    );
    if (!res.ok) {
      return { admitted: false, initialMessages: [], initialTokens: 0 };
    }
    const body = await res.json();
    return {
      admitted: true,
      initialMessages: Array.isArray(body.messages) ? body.messages : [],
      initialTokens:
        typeof body.total_tokens === "number" ? body.total_tokens : 0,
    };
  } catch {
    // Fail-soft: any upstream hiccup leaves the visitor at the gate.
    return { admitted: false, initialMessages: [], initialTokens: 0 };
  }
}
