import { error } from "@sveltejs/kit";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the public Grimoire read API. The public gateway has NO
// /api rule (the backend is never internet-reachable), so the browser calls this
// site route (/app/grimoire/api/<path>) and the fetch to the backend's
// /api/grimoire/<path> runs server-side here, mirroring the notes/ships/stars
// +server.js proxies. Streams the upstream body through so both the JSON read
// endpoints and the binary image endpoint work through one catch-all.
export async function GET({ params, url, fetch }) {
  const res = await fetch(
    `${API_BASE}/api/grimoire/${params.path}${url.search}`,
    { signal: AbortSignal.timeout(15_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "grimoire unavailable");
  }
  return new Response(res.body, {
    status: res.status,
    headers: {
      "content-type": res.headers.get("content-type") ?? "application/json",
      // The corpus is near-static; a minute of edge caching keeps origin hits
      // low without stalling coverage updates while extraction runs.
      "cache-control": "public, max-age=60",
    },
  });
}
