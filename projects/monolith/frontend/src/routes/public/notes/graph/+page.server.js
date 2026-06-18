import { error } from "@sveltejs/kit";
import {
  NOTES_PAGE_CACHE_CONTROL,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

// The localhost fallback is the established convention across every public
// +page.server.js proxy (ships/stars/notes/body); prod sets API_BASE via
// values.yaml.
// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Standalone full-screen public knowledge graph. Same visibility-filtered
// endpoint and cache posture as the /notes landing load; kept as its own route
// so the graph stays directly linkable after /notes became the chat front door
// (ADR 005). The backend enforces `visibility = 'public'` on both nodes and
// edges (both-ends-public), so we just pass the payload through.
export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/knowledge/public/graph`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "graph unavailable");
  }

  const headers = { "cache-control": NOTES_PAGE_CACHE_CONTROL };
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  const lastModified = res.headers?.get?.("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  setHeaders(headers);

  return { graph: await res.json() };
}
