// Client-side data access for the PUBLIC Grimoire (read-only tier). Every page
// is `ssr = false` and fetches through here against /api/grimoire (mounted only
// in main_public.py's router_public), matching the private grimoire's api.js
// pattern (lib/grimoire/api.js) but WITHOUT any campaign/viewpoint concept: the
// public corpus is a single global view, so there is no `?as=` query param and
// no campaign segment in any route.

// Same-origin proxy path, not the backend's /api/grimoire directly: the public
// gateway has no /api rule, so every read goes through the SvelteKit +server.js
// proxy at /app/grimoire/api/<path> (see routes/public/app/grimoire/api).
export const API = "/app/grimoire/api";

// The backend returns image_url as an absolute /api/grimoire/... path (it does
// not know about the public proxy); rewrite it onto the same-origin proxy so the
// browser reaches it through the gateway.
export const proxiedImageUrl = (imageUrl) =>
  imageUrl ? imageUrl.replace("/api/grimoire", API) : imageUrl;

export async function apiFetch(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    // Bound every corpus read so a stalled request never hangs the reader.
    signal: options.signal ?? AbortSignal.timeout(15_000),
  });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    throw new Error(body?.detail ?? `request failed (${res.status})`);
  }
  return body;
}

// Attribution the open licenses (CC BY 4.0 / ORC) require whenever a book's
// verbatim text is redistributed. Keyed by book_id and kept in sync with the
// backend's ingest.OPEN_LICENSE_BOOK_IDS: only these books are Reader-readable,
// so a missing key (copyrighted book) simply yields no credit line. Shown at
// the end of the continuous Reader.
export const BOOK_ATTRIBUTION = {
  "system-reference-doc-5-1":
    "Includes material from the System Reference Document 5.1, © Wizards of the Coast LLC, licensed under CC BY 4.0.",
  "system-reference-doc-5-2":
    "Includes material from the System Reference Document 5.2, © Wizards of the Coast LLC, licensed under CC BY 4.0.",
  "black-flag-reference-document":
    "Includes material from the Black Flag Reference Document, © Kobold Press, licensed under the ORC License and CC BY 4.0.",
  "a5e-srd":
    "Includes material from the Advanced 5e SRD (Level Up), © EN Publishing, licensed under the ORC License and CC BY 4.0.",
};

export const bookAttribution = (bookId) => BOOK_ATTRIBUTION[bookId] ?? "";

// ── Route builders (public URL structure: no [campaign], no ?as=) ──

export const homeHref = () => "/app/grimoire";
export const libraryHref = () => "/app/grimoire/library";
// World is the merged Entities+Explore surface. With an id it deep-links to
// that entity focused (the ?e= contract the dock, chat mentions, and the World
// page's own re-center all share); without one it lands on the featured entity.
export const worldHref = (id) =>
  id
    ? `/app/grimoire/world?e=${encodeURIComponent(id)}`
    : "/app/grimoire/world";
export const chatHref = () => "/app/grimoire/chat";
export const entityHref = (id) =>
  `/app/grimoire/entity/${encodeURIComponent(id)}`;
export const bookHref = (bookId) =>
  `/app/grimoire/book/${encodeURIComponent(bookId)}`;
export const chunkHref = (bookId, chunkId) =>
  `/app/grimoire/book/${encodeURIComponent(bookId)}/c/${encodeURIComponent(chunkId)}`;
export const adventureHref = (id) =>
  `/app/grimoire/adventure/${encodeURIComponent(id)}`;

// ── EXPLORE fetchers (router_public.py's /explore/* + /adventures) ──

// Induced subgraph {nodes, edges} for a scope + lens: the canvas's bulk load.
// scope is "everything" | "adventure:{id}" | "book:{id}"; lens is
// "world" | "story" | "quests" | "rules". See grimoire/explore.py's
// scope_subgraph for the exact node/edge projection.
export const exploreGraph = (scope, lens) =>
  apiFetch(
    `/explore/graph?scope=${encodeURIComponent(scope)}&lens=${encodeURIComponent(lens)}`,
  );

// Focus entity + its 1-hop is_global neighbors, same {nodes, edges,
// lens_counts} shape as exploreGraph, for click-to-expand ("wander") and the
// codex's relationship list. Query param is `id`, not `entity_id` (see
// router_public.py). scope/lens narrow the neighbor set the same way they
// narrow exploreGraph; the focus entity itself is always kept.
export const exploreEgo = (id, scope = "everything", lens = "world") =>
  apiFetch(
    `/explore/ego?id=${encodeURIComponent(id)}&scope=${encodeURIComponent(scope)}&lens=${encodeURIComponent(lens)}`,
  );

// Six-degrees BFS between two entities -> {path: [{entity, via}, ...]}.
// Wired here for completeness; no UI consumes it yet (deferred to a later
// pathfinding task).
export const explorePath = (from, to) =>
  apiFetch(
    `/explore/path?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
  );

// Every adventure across the whole corpus (not scoped to one book), for the
// EXPLORE scope selector.
export const listAllAdventures = () => apiFetch("/adventures");

// ── Entity list / search (router_public.py's /entities) ──

// Paginated entity list -> {items, total, next_cursor}. With no q and no type
// the backend orders by relationship degree (most-connected first); with a q or
// a type it orders by name. `signal` lets the caller abort a superseded
// typeahead request. Empty/whitespace q and type are dropped so an empty search
// falls back to the degree-ordered "everything" list.
export function listEntities({ q = "", type = "", limit = 40, signal } = {}) {
  const params = new URLSearchParams();
  if (type) params.set("type", type);
  if (q && q.trim()) params.set("q", q.trim());
  params.set("limit", String(limit));
  return apiFetch(`/entities?${params.toString()}`, signal ? { signal } : {});
}
