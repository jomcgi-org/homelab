import { error } from "@sveltejs/kit";

// SSR loader for a shared, read-only chat snapshot (ADR 005 "share this chat").
// The snapshot was minted server-side from the stored transcript; this route
// renders it read-only with no session, no input, and no Turnstile. The browser
// never talks to the internal chat API: SSR fetches the snapshot here.
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

// The public Turnstile site key gates "fork this chat" (a fork mints a new live
// session, same admission as starting a fresh chat). Public by design (it
// identifies the widget, not a credential); the Turnstile secret never enters
// SSR. Empty when unset, which disables the fork affordance gracefully.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

export async function load({ params, fetch }) {
  let resp;
  try {
    resp = await fetch(
      `${CHAT_API_BASE}/internal/chat/shared/${encodeURIComponent(params.id)}`,
      { signal: AbortSignal.timeout(10_000) },
    );
  } catch {
    // Treat an upstream hiccup as not-found rather than leaking a 5xx for a
    // public, link-shared page.
    throw error(404, "Shared chat not found");
  }

  if (resp.status === 404) {
    throw error(404, "Shared chat not found");
  }
  if (!resp.ok) {
    throw error(404, "Shared chat not found");
  }

  const snapshot = await resp.json();
  // Freeze the transcript: this view is immutable and read-only.
  return {
    snapshotId: snapshot.id,
    createdAt: snapshot.created_at ?? null,
    messages: Object.freeze(
      Array.isArray(snapshot.messages) ? snapshot.messages : [],
    ),
    turnstileSiteKey: TURNSTILE_SITE_KEY,
  };
}
