// Client-side data access for the PUBLIC Grimoire (read-only tier). Every page
// is `ssr = false` and fetches through here against /api/grimoire (mounted only
// in main_public.py's router_public), matching the private grimoire's api.js
// pattern (lib/grimoire/api.js) but WITHOUT any campaign/viewpoint concept: the
// public corpus is a single global view, so there is no `?as=` query param and
// no campaign segment in any route.

export const API = "/api/grimoire";

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

export const libraryHref = () => "/app/grimoire";
export const entitiesHref = () => "/app/grimoire/entities";
export const entityHref = (id) =>
  `/app/grimoire/entity/${encodeURIComponent(id)}`;
export const bookHref = (bookId) =>
  `/app/grimoire/book/${encodeURIComponent(bookId)}`;
export const chunkHref = (bookId, chunkId) =>
  `/app/grimoire/book/${encodeURIComponent(bookId)}/c/${encodeURIComponent(chunkId)}`;
