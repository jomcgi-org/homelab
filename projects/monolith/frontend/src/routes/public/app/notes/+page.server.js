// The public Turnstile site key gates the chat (ADR 005). It is PUBLIC by design
// (it identifies the widget, not a credential); the Turnstile *secret* never
// enters the frontend, only the FastAPI backend. Read from the environment.
//
// The graph payload is NOT loaded here: the page defaults to the chat view and
// only fetches the graph (client-side, via /app/notes/graph) when the visitor
// switches to the graph view, so the initial chat load stays light.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

export function load() {
  return { turnstileSiteKey: TURNSTILE_SITE_KEY };
}
