// Client-side admission helper for the Grimoire chat (ADR 005 pattern,
// grimoire_chat backend). Mirrors lib/public/chat/admission.js (the notes
// chat seam) exactly; only the proxy paths differ.
//
// The browser never talks to the internal chat API: it POSTs the solved
// Turnstile token to the same-origin SSR proxy (/app/grimoire/chat/session),
// which forwards the token + CF-Connecting-IP to the FastAPI backend, runs
// siteverify there, and sets the opaque httpOnly session cookie. This module
// is the single, testable seam for that call so the Svelte widget stays a
// thin shell around it.

// Same-origin SSR proxy paths (no reroute-hook mapping needed: these routes
// live directly under routes/public/app/grimoire/chat/).
const SESSION_PROXY_PATH = "/app/grimoire/chat/session";
const FORK_PROXY_PATH = "/app/grimoire/chat/fork";

/**
 * Exchange a solved Turnstile token for a server-side session.
 *
 * On success the SSR proxy sets the httpOnly `gcs` cookie; the opaque id never
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

/**
 * Fork a read-only shared snapshot into a new, continuable session.
 *
 * Same admission as {@link createChatSession} (a solved Turnstile token), plus
 * the snapshot id to seed the new session's history. On success the SSR proxy
 * sets the httpOnly `gcs` cookie; the caller then navigates to the live app,
 * which rehydrates the seeded transcript. The snapshot content is seeded
 * server-side, never from the browser.
 *
 * @param {string} snapshotId The shared snapshot to continue.
 * @param {string} turnstileToken The token the Turnstile widget produced.
 * @param {typeof fetch} [fetchImpl] Injectable fetch (defaults to global fetch).
 * @returns {Promise<{ ok: boolean, status: number }>}
 */
export async function forkChatSession(
  snapshotId,
  turnstileToken,
  fetchImpl = fetch,
) {
  const resp = await fetchImpl(FORK_PROXY_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      snapshot_id: snapshotId,
      turnstile_token: turnstileToken,
    }),
  });
  return { ok: resp.ok, status: resp.status };
}
