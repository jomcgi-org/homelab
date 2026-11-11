import {
  cloudflareCacheHeaders,
  PAGE_CACHE_CONTROL,
} from "$lib/cache-headers.js";
// Server-only import: post bodies stay out of the client bundle.
import manifest from "$lib/public/posts/posts-manifest.json";

// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// fallback would silently route to the wrong backend if the env var were missing
// (semgrep sveltekit-server-hardcoded-api-base-fallback).
const API_BASE = process.env.API_BASE;

// The homepage lists the latest published posts as legends under the keypad.
// Underscore-prefixed: SvelteKit allows only its own names as bare exports
// from a +page.server.js.
export const _HOMEPAGE_POSTS = 2;

export function _latestPosts(posts, limit = _HOMEPAGE_POSTS) {
  return [...posts]
    .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0))
    .slice(0, limit)
    .map(({ slug, title, date, summary }) => ({ slug, title, date, summary }));
}

async function _fetchJson(fetchFn, path) {
  try {
    const resp = await fetchFn(`${API_BASE}${path}`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

export async function load({ fetch, setHeaders }) {
  const stats = await _fetchJson(fetch, "/api/home/observability/stats");
  setHeaders(cloudflareCacheHeaders(PAGE_CACHE_CONTROL));
  return {
    stats,
    posts: _latestPosts(manifest),
  };
}
