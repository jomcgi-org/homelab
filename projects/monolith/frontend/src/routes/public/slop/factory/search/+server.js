import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  NOTES_PAGE_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function GET({ fetch, setHeaders, url }) {
  const upstream = new URL(`${API_BASE}/api/knowledge/public/search`);
  upstream.search = url.search;
  const res = await fetch(upstream.toString(), {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(
      res.status < 500 ? res.status : 503,
      "record search unavailable",
    );
  }

  const headers = cloudflareCacheHeaders(NOTES_PAGE_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  const lastModified = res.headers?.get?.("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  setHeaders(headers);

  return json(await res.json());
}
