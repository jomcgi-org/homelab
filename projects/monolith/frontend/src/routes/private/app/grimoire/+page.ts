// Everything on this page is fetched client-side against /api/grimoire (the
// campaign/viewpoint/entity picks are interactive state, not something a
// server load() would usefully pre-render), matching the private/notes and
// private/chat pages' pattern for this kind of app.
export const ssr = false;
