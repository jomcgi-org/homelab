import { error, json } from "@sveltejs/kit";
import {
  AGENT_ACTIVITY_CACHE_CONTROL,
  cloudflareCacheHeaders,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function GET({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/agents/public/activity`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "agent activity unavailable");
  }

  const headers = cloudflareCacheHeaders(AGENT_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return json(await res.json());
}
