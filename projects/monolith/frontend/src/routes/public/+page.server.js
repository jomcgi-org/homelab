import {
  cloudflareCacheHeaders,
  PAGE_CACHE_CONTROL,
} from "$lib/cache-headers.js";

// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// fallback would silently route to the wrong backend if the env var were missing
// (semgrep sveltekit-server-hardcoded-api-base-fallback).
const API_BASE = process.env.API_BASE;

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
  };
}
