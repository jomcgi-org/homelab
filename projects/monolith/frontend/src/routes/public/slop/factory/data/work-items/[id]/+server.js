import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../../../lib/cache-headers.js";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";
const WORK_ITEM_ID = /^[1-9][0-9]{5,18}$/;

export async function GET({ fetch, params, setHeaders }) {
  if (!WORK_ITEM_ID.test(params.id)) throw error(404, "no such work item");
  const res = await fetch(
    `${API_BASE}/api/agents/public/factory/work-items/${params.id}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(
      res.status === 404 ? 404 : 503,
      "factory work item unavailable",
    );
  }

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);
  return json(await res.json());
}
