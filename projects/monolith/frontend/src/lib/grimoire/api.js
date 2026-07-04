// Client-side data access + per-device preferences for the Grimoire app. Every
// page is `ssr = false` and fetches through here against /api/grimoire (the
// campaign/viewpoint/entity picks are interactive state, not something a server
// load() would usefully pre-render), matching the private/notes + private/chat
// pattern.

export const API = "/api/grimoire";

export async function apiFetch(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
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

// ── Per-device preferences (localStorage, guarded for SSR/private windows) ──

const LS_CAMPAIGN = "grimoire:campaign";
const LS_VIEWPOINT = "grimoire:viewpoint";

function readLS(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLS(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // ignore (private mode / SSR)
  }
}

export const lastCampaignId = () => readLS(LS_CAMPAIGN);
export const rememberCampaign = (id) => writeLS(LS_CAMPAIGN, id);
export const lastViewpoint = () => readLS(LS_VIEWPOINT);
export const rememberViewpoint = (v) => writeLS(LS_VIEWPOINT, v);

// "New since last visit" is a per-device signal: compare a book's latest_chunk_at
// against the timestamp we last recorded for that book on this device.
export const bookLastSeen = (bookId) => readLS(`grimoire:lastSeen:${bookId}`);
export const markBookSeen = (bookId, iso) =>
  writeLS(`grimoire:lastSeen:${bookId}`, iso ?? new Date().toISOString());

// ── Viewpoint (the `?as=` query param is the source of truth) ──

export const isDm = (viewpoint) => viewpoint === "dm";

// Effective viewpoint for a page: the URL wins; localStorage only seeds the
// default when the URL omits it; DM is the final fallback (matching the old
// loadCharacters semantics).
export function resolveViewpoint(url) {
  return url.searchParams.get("as") || lastViewpoint() || "dm";
}

// A query string carrying the viewpoint (plus any extras), for building API
// URLs and shareable in-app links.
export function asQuery(viewpoint, extra = {}) {
  return new URLSearchParams({ as: viewpoint, ...extra }).toString();
}

// ── Route builders (keep every in-app link viewpoint-carrying + shareable) ──

const base = (campaignId) => `/app/grimoire/${campaignId}`;

export const libraryHref = (campaignId, viewpoint) =>
  `${base(campaignId)}?${asQuery(viewpoint)}`;
export const entitiesHref = (campaignId, viewpoint) =>
  `${base(campaignId)}/entities?${asQuery(viewpoint)}`;
export const entityHref = (campaignId, id, viewpoint) =>
  `${base(campaignId)}/entity/${id}?${asQuery(viewpoint)}`;
export const bookHref = (campaignId, bookId, viewpoint) =>
  `${base(campaignId)}/book/${encodeURIComponent(bookId)}?${asQuery(viewpoint)}`;
export const chunkHref = (campaignId, bookId, chunkId, viewpoint) =>
  `${base(campaignId)}/book/${encodeURIComponent(bookId)}/c/${chunkId}?${asQuery(viewpoint)}`;
