// Same-origin proxy Reader.svelte's infinite scroll calls for every page
// after the first (which arrives pre-signed via the sibling +page.server.js
// load). Fetches the backend's /read directly (bypassing the gateway's
// /api/grimoire passthrough, which would stream image_key straight to the
// browser) and signs every image_key into image_url server-side, exactly like
// the page load does, so the signing secret never reaches the client either
// way.
import { error } from "@sveltejs/kit";
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
    throw error(res.status === 404 ? 404 : 503, "grimoire unavailable");
  }
  const page = signReadPage(await res.json());
  return new Response(JSON.stringify(page), {
    headers: { "content-type": "application/json" },
  });
}
