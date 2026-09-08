import {
  cloudflareCacheHeaders,
  NOTES_PAGE_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

export const prerender = false;

const SLUG = /^[a-z0-9][a-z0-9-]{0,79}$/;
// Below this an entity has too few atoms to be worth a line in the index.
const MIN_INDEX_ATOMS = 15;

async function getJson(fetch, path, label) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${label} unavailable`);
  return { data: await response.json(), response };
}

export async function load({ fetch, setHeaders, url }) {
  const requestedEntity = url.searchParams.get("entity") || "";
  const entity = SLUG.test(requestedEntity) ? requestedEntity : "";
  const q = (url.searchParams.get("q") || "").trim().slice(0, 200);
  const baseSections = await Promise.allSettled([
    getJson(fetch, "/slop/factory/entities", "record index"),
    getJson(fetch, "/slop/factory/facts", "fact history"),
  ]);
  const entities =
    baseSections[0].status === "fulfilled" ? baseSections[0].value.data : [];
  const facts =
    baseSections[1].status === "fulfilled"
      ? baseSections[1].value.data
      : {
          daily: [],
          totals: { verified: 0, unverified: 0, disputed: 0 },
          contradictions: 0,
        };
  const projects = entities
    .filter(
      (item) =>
        item.kind === "project" &&
        (item.note_counts?.verified ?? 0) +
          (item.note_counts?.unverified ?? 0) >
          MIN_INDEX_ATOMS,
    )
    .sort(
      (a, b) =>
        (b.note_counts?.verified ?? 0) +
          (b.note_counts?.unverified ?? 0) -
          ((a.note_counts?.verified ?? 0) + (a.note_counts?.unverified ?? 0)) ||
        a.title.localeCompare(b.title),
    );

  let contentPromise = Promise.resolve(null);
  if (q) {
    const params = new URLSearchParams({ q, limit: "30" });
    contentPromise = getJson(
      fetch,
      `/slop/factory/search?${params}`,
      "record search",
    );
  } else if (entity) {
    contentPromise = getJson(
      fetch,
      `/slop/factory/entities/project/${encodeURIComponent(entity)}/notes?state=verified%2Cunverified%2Cdisputed&limit=60`,
      "record chapter",
    );
  }
  const [contentSection] = await Promise.allSettled([contentPromise]);
  const content =
    contentSection.status === "fulfilled" ? contentSection.value : null;
  const chapter = entity && !q ? (content?.data ?? null) : null;
  const results = q ? (content?.data ?? []) : [];

  const headers = cloudflareCacheHeaders(NOTES_PAGE_CACHE_CONTROL);
  const validators = [
    ...baseSections
      .filter((section) => section.status === "fulfilled")
      .map((section) => section.value.response),
    content?.response,
  ]
    .filter(Boolean)
    .map((response) => response.headers?.get?.("etag"))
    .filter(Boolean)
    .join("-");
  const etag = versionedEtag(validators);
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return {
    title: "Factory context",
    entities,
    facts,
    projects,
    entity,
    q,
    chapter,
    results,
    unavailable: {
      entities: baseSections[0].status === "rejected",
      facts: baseSections[1].status === "rejected",
      chapter: Boolean(entity && !q && contentSection.status === "rejected"),
      search: Boolean(q && contentSection.status === "rejected"),
    },
  };
}
