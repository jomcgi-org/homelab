import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../../../lib/cache-headers.js";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// A session key is `factory:<task>:<node>:<attempt>` and a node key may itself
// carry colons, so the key travels as one rest parameter. SvelteKit hands it
// back decoded; it is encoded once more on the way upstream so the colons stay
// inside a single path segment there too.
export async function GET({ fetch, params, setHeaders }) {
  const key = params.key ?? "";
  if (!key) {
    throw error(404, "no such session");
  }
  const res = await fetch(
    `${API_BASE}/api/agents/public/factory/sessions/${encodeURIComponent(key)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "factory session unavailable");
  }

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return json(await res.json());
}
