import { error } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  NOTES_PAGE_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

export const prerender = false;

const SLUG = /^[a-z0-9][a-z0-9-]{0,79}$/;

async function getJson(fetch, path, label) {
  const response = await fetch(path);
  if (!response.ok)
    throw error(response.status === 404 ? 404 : 503, `${label} unavailable`);
  return { data: await response.json(), response };
}

export async function load({ fetch, setHeaders, url }) {
  const requestedEntity = url.searchParams.get("entity") || "";
  const entity = SLUG.test(requestedEntity) ? requestedEntity : "";
  const q = (url.searchParams.get("q") || "").trim().slice(0, 200);
  const mode =
    url.searchParams.get("mode") === "semantic" ? "semantic" : "grep";
  const catalog = await getJson(
    fetch,
    "/slop/factory/entities",
    "record index",
  );
  const projects = catalog.data
    .filter((item) => item.kind === "project")
    .sort(
      (a, b) =>
        (b.note_counts?.verified ?? 0) +
          (b.note_counts?.unverified ?? 0) -
          ((a.note_counts?.verified ?? 0) + (a.note_counts?.unverified ?? 0)) ||
        a.title.localeCompare(b.title),
    );

  let chapter = null;
  let results = [];
  let contentResponse = null;
  if (q) {
    const params = new URLSearchParams({ q, mode, limit: "30" });
    const search = await getJson(
      fetch,
      `/slop/factory/search?${params}`,
      "record search",
    );
    results = search.data;
    contentResponse = search.response;
  } else if (entity) {
    if (!projects.some((project) => project.slug === entity)) {
      throw error(404, "record chapter unavailable");
    }
    const notes = await getJson(
      fetch,
      `/slop/factory/entities/project/${encodeURIComponent(entity)}/notes?state=verified%2Cunverified&limit=60`,
      "record chapter",
    );
    chapter = notes.data;
    contentResponse = notes.response;
  }

  const headers = cloudflareCacheHeaders(NOTES_PAGE_CACHE_CONTROL);
  const validators = [catalog.response, contentResponse]
    .filter(Boolean)
    .map((response) => response.headers?.get?.("etag"))
    .filter(Boolean)
    .join("-");
  const etag = versionedEtag(validators);
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return {
    title: "Factory record",
    entities: catalog.data,
    projects,
    entity,
    q,
    mode,
    chapter,
    results,
  };
}
