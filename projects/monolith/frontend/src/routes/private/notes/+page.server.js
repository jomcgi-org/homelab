import { error } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  NOTES_PAGE_CACHE_CONTROL,
  versionedEtag,
} from "../../../lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/knowledge/graph`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "graph unavailable");
  }

  const headers = cloudflareCacheHeaders(NOTES_PAGE_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  const lastModified = res.headers?.get?.("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  setHeaders(headers);

  return { graph: await res.json() };
}
