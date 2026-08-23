import { error } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  GRIMOIRE_READ_CACHE_CONTROL,
} from "$lib/cache-headers.js";

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
    // Forward the backend's own 404/403 rather than masking them as a generic
    // 503: 403 is the deliberate license gate on copyrighted books (see
    // router_public.read_book), and reporting it as "unavailable" reads like an
    // outage and trips 5xx alerting. 503 stays for real upstream failures.
    const status = res.status === 404 ? 404 : res.status === 403 ? 403 : 503;
    throw error(
      status,
      status === 403
        ? "this book is not available to read publicly"
        : "grimoire unavailable",
    );
  }
  return new Response(res.body, {
    status: res.status,
    headers: {
      ...cloudflareCacheHeaders(GRIMOIRE_READ_CACHE_CONTROL),
      "content-type": res.headers.get("content-type") ?? "application/json",
      // The corpus is a read-only, near-static book library, so it takes an
      // aggressive 1 h edge cache (CDN offload that keeps a share-driven spike
      // off the origin). max-age=0 keeps the browser revalidating so new
      // coverage lands within the hour. See GRIMOIRE_READ_CACHE_CONTROL.
    },
  });
}
