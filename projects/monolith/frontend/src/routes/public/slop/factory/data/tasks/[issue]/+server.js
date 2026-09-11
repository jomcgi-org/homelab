import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../../../lib/cache-headers.js";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// A task is named by its GitHub issue number and nothing else, so anything that
// is not a plain number is a 404 here rather than a request the API has to
// reject.
const ISSUE = /^[0-9]{1,12}$/;

export async function GET({ fetch, params, setHeaders }) {
  if (!ISSUE.test(params.issue)) {
    throw error(404, "no such task");
  }
  const res = await fetch(
    `${API_BASE}/api/agents/public/factory/tasks/${params.issue}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "factory task unavailable");
  }

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return json(await res.json());
}
