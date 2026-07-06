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

// ── Route builders (public URL structure: no [campaign], no ?as=) ──

export const homeHref = () => "/app/grimoire";
export const libraryHref = () => "/app/grimoire/library";
export const entitiesHref = () => "/app/grimoire/entities";
export const exploreHref = () => "/app/grimoire/explore";
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

// Focus entity + its 1-hop is_global neighbors, same {nodes, edges} shape as
// exploreGraph, for click-to-expand ("wander") and the codex's relationship
// list. Query param is `id`, not `entity_id` (see router_public.py).
export const exploreEgo = (id) =>
  apiFetch(`/explore/ego?id=${encodeURIComponent(id)}`);

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
