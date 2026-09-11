import { error } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  FACTORY_ACTIVITY_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../../../../lib/cache-headers.js";

export const prerender = false;

export async function load({ fetch, params, setHeaders }) {
  // The session key is not derivable from the URL: it is minted per attempt and
  // recorded on it, so the task comes first and names the session to fetch.
  // SvelteKit has already decoded the [node] segment, so the key it holds is
  // the node key as the engine wrote it, colons and all.
  const taskResponse = await fetch(
    `/slop/factory/data/tasks/${encodeURIComponent(params.issue)}`,
  );
  if (taskResponse.status === 404) throw error(404, "No such task");
  if (!taskResponse.ok) throw error(503, "factory task unavailable");
  const taskPayload = await taskResponse.json();
  const task = taskPayload.task;

  const attempts = (task?.nodes ?? [])
    .filter((node) => node.node_key === params.node)
    .flatMap((node) =>
      (node.attempts ?? []).map((attempt) => ({ node, attempt })),
    );
  const found = attempts.find(
    (entry) => String(entry.attempt.attempt) === params.attempt,
  );
  if (!found?.attempt.session_key) throw error(404, "No such session");

  const sessionResponse = await fetch(
    `/slop/factory/data/sessions/${encodeURIComponent(found.attempt.session_key)}`,
  );
  if (sessionResponse.status === 404) throw error(404, "No such session");
  if (!sessionResponse.ok) throw error(503, "factory session unavailable");
  const sessionPayload = await sessionResponse.json();

  const headers = cloudflareCacheHeaders(FACTORY_ACTIVITY_CACHE_CONTROL);
  const etag = versionedEtag(sessionResponse.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return {
    title: `${params.node} attempt ${params.attempt}`,
    policy: taskPayload.policy ?? {},
    task,
    node: found.node,
    attempt: found.attempt,
    session: sessionPayload.session,
    turns: sessionPayload.turns ?? [],
  };
}
