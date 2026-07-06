import { error } from "@sveltejs/kit";

// SSR loader for a shared, read-only Grimoire chat snapshot (ADR 005 "share
// this chat", ported to the grimoire_chat backend). The snapshot was minted
// server-side from the stored transcript; this route renders it read-only
// with no session, no input, and no Turnstile. The browser never talks to the
// internal chat API: SSR fetches the snapshot here. The localhost fallback is
// the established convention across every public proxy/loader
// (ships/stars/notes/campsites/grimoire); prod sets CHAT_API_BASE via
// values.yaml.
// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const CHAT_API_BASE = process.env.CHAT_API_BASE || "http://localhost:8000";

// The public Turnstile site key gates "fork this chat" (a fork mints a new
// live session, same admission as starting a fresh chat). Public by design
// (it identifies the widget, not a credential); the Turnstile secret never
// enters SSR. Empty when unset, which disables the fork affordance gracefully.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

export async function load({ params, fetch }) {
  let resp;
  try {
    resp = await fetch(
      `${CHAT_API_BASE}/internal/grimoire-chat/shared/${encodeURIComponent(params.id)}`,
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
