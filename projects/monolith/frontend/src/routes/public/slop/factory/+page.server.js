import {
  AGENT_ACTIVITY_CACHE_CONTROL,
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
  ];
  const values = sections.map((section, index) =>
    section.status === "fulfilled" ? section.value.data : fallback[index],
  );

  const headers = cloudflareCacheHeaders(AGENT_ACTIVITY_CACHE_CONTROL);
  const validators = sections
    .filter((section) => section.status === "fulfilled")
    .map((section) => section.value.response.headers?.get?.("etag"))
    .filter(Boolean)
    .join("-");
  const etag = versionedEtag(validators);
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return {
    title: "Factory",
    activity: values[0],
    merges: values[1],
    facts: values[2],
    unavailable: {
      activity: sections[0].status === "rejected",
      merges: sections[1].status === "rejected",
      facts: sections[2].status === "rejected",
    },
  };
}
