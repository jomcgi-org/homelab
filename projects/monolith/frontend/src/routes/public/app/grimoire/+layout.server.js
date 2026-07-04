// The public Turnstile site key gates the whole Grimoire app (mirrors the
// /app/notes and /public/chat pattern: process.env.TURNSTILE_SITE_KEY read
// server-side, ssr=false at the sibling +layout.js so the corpus itself never
// renders server-side). The site key is PUBLIC by design (it identifies the
// widget, not a credential); the Turnstile *secret* never leaves the FastAPI
// backend.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

export function load() {
  return { turnstileSiteKey: TURNSTILE_SITE_KEY };
}
