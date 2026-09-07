import { error } from "@sveltejs/kit";
import {
  AGENT_ACTIVITY_CACHE_CONTROL,
  cloudflareCacheHeaders,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

export const prerender = false;

async function read(response, label) {
  if (!response.ok) throw error(503, `${label} unavailable`);
  return response.json();
}

export async function load({ fetch, setHeaders }) {
  const responses = await Promise.all([
    fetch("/slop/factory/activity"),
    fetch("/slop/factory/merges"),
    fetch("/slop/factory/entities"),
  ]);
  const [activity, merges, entities] = await Promise.all([
    read(responses[0], "agent activity"),
    read(responses[1], "merge snapshot"),
    read(responses[2], "record index"),
  ]);
  const projectResponses = await Promise.all(
    entities
      .filter((entity) => entity.kind === "project")
      .map((entity) =>
        fetch(
          `/slop/factory/entities/project/${encodeURIComponent(entity.slug)}/notes?state=verified%2Cunverified&limit=60`,
        ),
      ),
  );
  const chapters = await Promise.all(
    projectResponses.map((response) => read(response, "record chapter")),
  );
  const factMap = new Map();
  for (const chapter of chapters) {
    for (const note of chapter.notes) factMap.set(note.note_id, note);
  }

  const headers = cloudflareCacheHeaders(AGENT_ACTIVITY_CACHE_CONTROL);
  const validators = [...responses, ...projectResponses]
    .map((response) => response.headers?.get?.("etag"))
    .filter(Boolean)
    .join("-");
  const etag = versionedEtag(validators);
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return {
    title: "Factory",
    activity,
    merges,
    entities,
    facts: [...factMap.values()],
  };
}
