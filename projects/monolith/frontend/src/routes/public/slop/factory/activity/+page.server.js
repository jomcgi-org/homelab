import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

// Restate the default so this lane is not prerendered if a parent enables it.
export const prerender = false;

// The board is the whole page, so a failed fetch still renders: the masthead
// and an unavailable line, the same shape the overview uses. An empty ledger
// and an outage should not look alike, hence the flag rather than a bare [].
const EMPTY_BOARD = {
  snapshotted_at: null,
  state: "unknown",
  policy: {},
  active: [],
  queued: [],
  recent: [],
};

export async function load({ fetch, setHeaders }) {
  const response = await fetch("/slop/factory/data/board");
  const ok = response.ok;
  const board = ok ? await response.json() : EMPTY_BOARD;

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = ok ? versionedEtag(response.headers?.get?.("etag")) : undefined;
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return {
    title: "Factory activity",
    board,
    unavailable: !ok,
  };
}
