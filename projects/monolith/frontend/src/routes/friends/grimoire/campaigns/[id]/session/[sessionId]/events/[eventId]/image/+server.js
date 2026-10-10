import { error } from "@sveltejs/kit";
import { grimoireHeaders } from "$lib/server/grimoire-auth.js";

const UUID = /^[0-9a-f-]{36}$/i;

// Stream a handout image through the signed-in member's own token so the
// backend audience predicate decides who sees the bytes. Each route param is
// one encoded path segment; nothing else reaches the backend path.
export async function GET({ fetch, cookies, params }) {
  if (process.env.GRIMOIRE_PLAY_ENABLED !== "true")
    error(404, "Session play is not enabled.");
  for (const id of [params.id, params.sessionId, params.eventId])
    if (!UUID.test(id)) error(404, "Handout image not found.");
  let headers;
  try {
    headers = grimoireHeaders(cookies);
  } catch {
    error(401, "Your session has expired. Please sign in again.");
  }
  const upstream = await fetch(
    `${process.env.API_BASE}/api/grimoire/campaigns/${encodeURIComponent(params.id)}/sessions/${encodeURIComponent(params.sessionId)}/events/${encodeURIComponent(params.eventId)}/image`,
    { signal: AbortSignal.timeout(15_000), headers },
  );
  if (!upstream.ok)
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type":
          upstream.headers.get("content-type") || "application/json",
        "cache-control": "private, no-store",
      },
    });
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": upstream.headers.get("content-type") || "image/png",
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
