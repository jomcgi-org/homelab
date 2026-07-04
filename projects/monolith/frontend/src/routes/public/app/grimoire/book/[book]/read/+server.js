// Same-origin proxy the public Reader.svelte's infinite scroll calls for
// every page after the first. Mirrors the private tier's sibling route: talks
// to the backend directly (not the generic api/[...path] catch-all, which
// streams image_key straight through unsigned) and signs every image_key into
// image_url server-side.
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
