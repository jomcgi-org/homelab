// Server-loads the first page of the continuous reader for one book. Runs on
// the server even though the grimoire subtree has `ssr = false` (server load
// functions always execute server-side; ssr=false only skips rendering the
// resulting HTML), which is what lets this sign image_keys into image_url via
// $lib/server/grimoire-img.js without ever shipping IMGPROXY_KEY/SALT or the
// raw sign logic to the browser. Further pages are fetched client-side by
// Reader.svelte through the sibling read/+server.js proxy, which does the
// same signing for infinite scroll.
//
// `from` is the deep-link entry cursor: the /c/[chunk] redirect resolver
// computes it (seq - 1) so the returned page starts exactly at that chunk.
// Talks to the backend directly (API_BASE), not the gateway's /api/grimoire
// passthrough: this load must run signing logic no gateway rule can do.
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
