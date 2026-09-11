import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../../lib/cache-headers.js";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// The factory board: the policy in force plus the active, queued and recently
// finished tasks. The browser never calls the API itself, so this same-origin
// proxy is what both the SSR load and the 60 s refetch reach.
export async function GET({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/agents/public/factory/activity`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "factory activity unavailable");
  }

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return json(await res.json());
}
