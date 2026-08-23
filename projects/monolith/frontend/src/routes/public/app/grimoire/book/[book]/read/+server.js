// Same-origin proxy the public Reader.svelte's infinite scroll calls for
// every page after the first. Mirrors the private tier's sibling route: talks
// to the backend directly (not the generic api/[...path] catch-all, which
// streams image_key straight through unsigned) and signs every image_key into
// image_url server-side.
import { error } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  GRIMOIRE_READ_CACHE_CONTROL,
} from "$lib/cache-headers.js";
import { apiBase, signReadPage } from "$lib/server/grimoire-read.js";

export async function GET({ params, url, fetch }) {
  const bookId = decodeURIComponent(params.book);
  const qs = new URLSearchParams();
  const cursor = url.searchParams.get("cursor");
  const limit = url.searchParams.get("limit");
  if (cursor) qs.set("cursor", cursor);
  if (limit) qs.set("limit", limit);
  const suffix = qs.toString() ? `?${qs}` : "";
  const res = await fetch(
    `${apiBase()}/api/grimoire/books/${encodeURIComponent(bookId)}/read${suffix}`,
    { signal: AbortSignal.timeout(15_000) },
  );
  if (!res.ok) {
    // Forward the backend's 404/403 (403 = the copyrighted-book license gate)
    // instead of collapsing to a misleading 503. See the api/[...path] proxy.
    const status = res.status === 404 ? 404 : res.status === 403 ? 403 : 503;
    throw error(
      status,
      status === 403
        ? "this book is not available to read publicly"
        : "grimoire unavailable",
    );
  }
  const page = signReadPage(await res.json());
  return new Response(JSON.stringify(page), {
    headers: {
      ...cloudflareCacheHeaders(GRIMOIRE_READ_CACHE_CONTROL),
      "content-type": "application/json",
      // Edge-cache the reader pages like the api/[...path] proxy: the signed
      // image_url is a deterministic, non-expiring HMAC (grimoire-img.js), so a
      // 1 h page cache never serves a stale signature. See
      // GRIMOIRE_READ_CACHE_CONTROL.
    },
  });
}
