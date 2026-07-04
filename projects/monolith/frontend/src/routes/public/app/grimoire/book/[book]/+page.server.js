// Server-loads the first page of the continuous public reader for one book.
// Mirrors the private tier's book +page.server.js (see that file's docblock
// for why a server load still runs here even though the grimoire subtree is
// ssr=false): this is what lets image_key get signed into image_url via
// $lib/server/grimoire-img.js without the signing secret ever reaching the
// browser. No campaign/viewpoint concept on this tier — the corpus is a
// single global view. Talks to the backend directly (API_BASE), not the
// generic api/[...path] catch-all proxy: that proxy is a dumb stream and does
// not sign images.
import { error } from "@sveltejs/kit";
import { apiBase, signReadPage } from "$lib/server/grimoire-read.js";

export async function load({ params, url, fetch }) {
  const bookId = decodeURIComponent(params.book);
  const cursor = url.searchParams.get("from");
  const qs = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  const res = await fetch(
    `${apiBase()}/api/grimoire/books/${encodeURIComponent(bookId)}/read${qs}`,
    { signal: AbortSignal.timeout(15_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "grimoire unavailable");
  }
  const page = signReadPage(await res.json());
  return { bookId, items: page.items, nextCursor: page.next_cursor };
}
