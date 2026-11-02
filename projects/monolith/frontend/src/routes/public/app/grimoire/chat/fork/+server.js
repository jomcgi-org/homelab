import { mintSession } from "$lib/server/grimoire-chat-session.js";

// Server-only BFF for "fork this chat" (began as a copy of the retired
// notes-chat proxy). A shared
// snapshot is read-only; forking it mints a NEW server-side session seeded
// with the snapshot's frozen transcript so the visitor can continue it. The
// browser POSTs here same-origin (/app/grimoire/chat/fork) with a solved
// Turnstile token + the snapshot id; this handler forwards them to the
// internal grimoire_chat API, which verifies the challenge (the Turnstile
// secret lives in the backend, never SSR) and seeds the new session
// SERVER-SIDE from the stored snapshot. On success the opaque session id
// lands in the httpOnly "gcs" cookie, never the body, exactly like
// /app/grimoire/chat/session. The browser never talks to the internal
// grimoire_chat API directly.

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

  // On success the client navigates to the live app, which rehydrates the
  // seeded transcript.
  return mintSession({ request, cookies }, "/internal/grimoire-chat/fork", {
    snapshot_id: snapshotId,
    turnstile_token: turnstileToken,
  });
}
