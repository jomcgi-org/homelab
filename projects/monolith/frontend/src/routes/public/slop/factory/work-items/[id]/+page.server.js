import { error } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../../lib/cache-headers.js";

export const prerender = false;

export async function load({ fetch, params, setHeaders }) {
  const response = await fetch(
    `/slop/factory/data/work-items/${encodeURIComponent(params.id)}`,
  );
  if (response.status === 404) throw error(404, "No such work item");
  if (!response.ok) throw error(503, "factory work item unavailable");
  const document = await response.json();

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(response.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);
  return { document };
}
