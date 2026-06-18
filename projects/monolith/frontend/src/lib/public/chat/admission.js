// Client-side admission helper for public chat (ADR 005, Phase 2).
//
// The browser never talks to the internal chat API: it POSTs the solved
// Turnstile token to the same-origin SSR proxy (/chat/session, rerouted to
// /public/chat/session), which forwards the token + CF-Connecting-IP to the
// FastAPI backend, runs siteverify there, and sets the opaque httpOnly session
// cookie. This module is the single, testable seam for that call so the Svelte
// widget stays a thin shell around it.

// Same-origin SSR proxy path (the reroute hook maps it to /public/chat/session).
const SESSION_PROXY_PATH = "/chat/session";

/**
 * Exchange a solved Turnstile token for a server-side session.
 *
 * On success the SSR proxy sets the httpOnly `cps` cookie; the opaque id never
 * reaches the browser, so this returns only an `ok` flag plus the HTTP status.
 *
 * @param {string} turnstileToken The token the Turnstile widget produced.
 * @param {typeof fetch} [fetchImpl] Injectable fetch (defaults to global fetch).
 * @returns {Promise<{ ok: boolean, status: number }>}
 */
export async function createChatSession(turnstileToken, fetchImpl = fetch) {
  const resp = await fetchImpl(SESSION_PROXY_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ turnstile_token: turnstileToken }),
  });
  return { ok: resp.ok, status: resp.status };
}
