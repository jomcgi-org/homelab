import { error } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../../lib/cache-headers.js";

export const prerender = false;

export async function load({ fetch, params, setHeaders }) {
  const response = await fetch(
    `/slop/factory/data/tasks/${encodeURIComponent(params.issue)}`,
  );
  // A task that is not in the ledger is a wrong URL, not an outage, so it gets
  // the error page rather than an empty walkthrough.
  if (response.status === 404) throw error(404, "No such task");
  if (!response.ok) throw error(503, "factory task unavailable");
  const payload = await response.json();

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(response.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return {
    title: `Task #${params.issue}`,
    snapshottedAt: payload.snapshotted_at ?? null,
    policy: payload.policy ?? {},
    task: payload.task,
  };
}
