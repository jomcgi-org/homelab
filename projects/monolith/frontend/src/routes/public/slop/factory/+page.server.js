import {
  boundedCacheHeaders,
  cloudflareCacheHeaders,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

export const prerender = false;

async function getJson(fetch, path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path} unavailable`);
  return { data: await response.json(), response };
}

export async function load({ fetch, setHeaders }) {
  const sections = await Promise.allSettled([
    getJson(fetch, "/slop/factory/data/activity"),
    getJson(fetch, "/slop/factory/merges"),
    getJson(fetch, "/slop/factory/facts"),
    getJson(fetch, "/slop/factory/goals"),
    getJson(fetch, "/slop/factory/data/board"),
  ]);
  const fallback = [
    {
      now: {},
      daily: [],
      local_daily: [],
      spend_daily: [],
      totals_7d: { ember: {}, local: {}, combined: {} },
    },
    { daily: [], week: [], totals: {}, snapshotted_at: null },
    {
      daily: [],
      totals: { verified: 0, unverified: 0, disputed: 0 },
      contradictions: 0,
    },
    { goals: [], declared_at: null, stale: true },
    { state: "unknown", policy: {}, active: [], queued: [], recent: [] },
  ];
  const values = sections.map((section, index) =>
    section.status === "fulfilled" ? section.value.data : fallback[index],
  );

  // A composed response cannot outlive any constituent or cache a fallback.
  const policies = sections.map((section) =>
    section.status === "fulfilled"
      ? boundedCacheHeaders(
          "public, max-age=60, s-maxage=60, must-revalidate",
          section.value.response.headers,
        )
      : cloudflareCacheHeaders("no-store"),
  );
  const ttl = Math.min(
    ...policies.map((policy) =>
      Number(policy["cache-control"].match(/s-maxage=(\d+)/)?.[1] || 0),
    ),
  );
  const headers = cloudflareCacheHeaders(
    ttl > 0
      ? `public, max-age=${ttl}, s-maxage=${ttl}, must-revalidate`
      : "no-store",
  );
  if (ttl > 0) headers.date = new Date().toUTCString();
  const validators = sections
    .filter((section) => section.status === "fulfilled")
    .map((section) => section.value.response.headers?.get?.("etag"))
    .filter(Boolean)
    .join("-");
  const etag = versionedEtag(validators);
  if (etag && ttl > 0) headers.etag = etag;
  setHeaders(headers);

  return {
    evaluatedAt: new Date().toISOString(),
    title: "Factory",
    activity: values[0],
    merges: values[1],
    facts: values[2],
    goals: values[3],
    board: values[4],
    unavailable: {
      activity: sections[0].status === "rejected",
      merges: sections[1].status === "rejected",
      facts: sections[2].status === "rejected",
      goals: sections[3].status === "rejected",
      board: sections[4].status === "rejected",
    },
  };
}
