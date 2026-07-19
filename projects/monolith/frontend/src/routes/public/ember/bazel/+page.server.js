// The public Turnstile site key gates the query session on this demo (see
// ADR embervm/010 and ember_public/bazel_router.py). It is PUBLIC by design
// (it identifies the widget, not a credential); the Turnstile *secret* never
// enters the frontend, only the FastAPI backend. Same convention as
// routes/public/ember/postgres/+page.server.js.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

export async function load() {
  // No status endpoint for this demo (see plan Task 7): each query is a
  // one-shot task-class Assign, there is no VM lifecycle to poll.
  return {
    turnstileSiteKey: TURNSTILE_SITE_KEY,
  };
}
