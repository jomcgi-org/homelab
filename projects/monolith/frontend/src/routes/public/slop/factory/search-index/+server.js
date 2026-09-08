import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  SEARCH_INDEX_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function GET({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/knowledge/public/search-index`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "record search index unavailable");
  }

  const headers = cloudflareCacheHeaders(SEARCH_INDEX_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  const lastModified = res.headers?.get?.("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  setHeaders(headers);

  return json(await res.json());
}
