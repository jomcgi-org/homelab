// The whole Grimoire subtree is client-only: campaign/viewpoint/entity picks are
// interactive state fetched against /api/grimoire, not something a server load()
// would usefully pre-render (same rationale as private/notes + private/chat).
// Declaring ssr = false at the layout covers the index and every [campaign] route.
export const ssr = false;
