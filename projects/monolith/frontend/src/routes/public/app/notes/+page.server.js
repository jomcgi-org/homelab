// The public Turnstile site key gates the chat (ADR 005). It is PUBLIC by design
// (it identifies the widget, not a credential); the Turnstile *secret* never
// enters the frontend, only the FastAPI backend. Read from the environment.
//
// The graph payload is NOT loaded here: the page defaults to the chat view and
// only fetches the graph (client-side, via /app/notes/graph) when the visitor
// switches to the graph view, so the initial chat load stays light.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

export async function load({ fetch }) {
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

  return { turnstileSiteKey: TURNSTILE_SITE_KEY, stats };
}
