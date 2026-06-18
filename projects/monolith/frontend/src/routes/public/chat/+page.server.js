// Expose the public Turnstile site key to the client (ADR 005, Phase 2).
//
// Consistent with how this app passes server config to the client elsewhere
// (e.g. +page.server.js reading process.env.API_BASE): the SSR load reads the
// site key from the environment and returns it in the page data. The site key
// is PUBLIC by design (it identifies the widget, not a credential); the
// Turnstile *secret* never enters the frontend, only the FastAPI backend.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

export function load() {
  return { turnstileSiteKey: TURNSTILE_SITE_KEY };
}
