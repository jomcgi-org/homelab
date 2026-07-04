// Deep-link resolver: entity-mention links, search hits, and the flat TOC all
// still point at /book/[book]/c/[chunk] (never broken), but the chunk is no
// longer read on its own page. This looks the chunk up for its seq (the only
// thing the continuous reader needs), then redirects into the book reader
// positioned exactly there via a `from` cursor (seq - 1, so /read's `seq >
// cursor` keyset page starts on this chunk) plus a `#c-<chunkId>` anchor the
// reader scrolls to after its first render.
import { error, redirect } from "@sveltejs/kit";
import { apiBase } from "$lib/server/grimoire-read.js";

export async function load({ params, url, fetch }) {
  const campaignId = params.campaign;
  const bookId = decodeURIComponent(params.book);
  const chunkId = params.chunk;
  const viewpoint = url.searchParams.get("as") || "dm";

  const res = await fetch(
    `${apiBase()}/api/grimoire/chunks/${encodeURIComponent(chunkId)}` +
      `?campaign=${encodeURIComponent(campaignId)}&as=${encodeURIComponent(viewpoint)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "chunk not found");
  }
  const chunk = await res.json();

  const qs = new URLSearchParams({ as: viewpoint });
  // A null seq (nullable column, loader/backfill-set edge case) can't be
  // turned into a cursor; fall back to the book's first page rather than a
  // "None" cursor. The #c-<id> anchor is still included, but it will only
  // scroll into view if that chunk happens to land on the first page.
  if (chunk.seq != null) qs.set("from", String(chunk.seq - 1));

  throw redirect(
    307,
    `/app/grimoire/${encodeURIComponent(campaignId)}/book/${encodeURIComponent(bookId)}?${qs}#c-${encodeURIComponent(chunkId)}`,
  );
}
