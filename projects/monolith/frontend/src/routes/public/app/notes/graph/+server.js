import { error, json } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. This endpoint sits at
// routes/public/app/notes/graph, so five ../ segments reach src/lib.
import {
  cloudflareCacheHeaders,
  NOTES_PAGE_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

// The localhost fallback is the established convention across every public
// proxy (ships/stars/notes/body); prod sets API_BASE via values.yaml.
// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the public knowledge graph. The notes app fetches this
// site route (/app/notes/graph) the first time a visitor opens the graph view;
// the fetch to the backend's visibility-filtered /api/knowledge/public/graph
// happens server-side here, so the browser never calls the backend directly.
// This keeps the public API off the internet (the gateway only routes to the
// frontend; mirrors the ships/stars/body +server.js proxies). The backend
// enforces `visibility = 'public'` on both nodes and edges (both-ends-public),
// so we just pass the payload through.
export async function GET({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/knowledge/public/graph`, {
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

  return json(await res.json());
}
