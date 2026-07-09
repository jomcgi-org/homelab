// Deep-link resolver: entity-mention links and search hits still point at
// /book/[book]/c/[chunk] (never broken), but the chunk is no longer read on
// its own page. Looks the chunk up for its seq, then redirects into the
// continuous reader positioned there via a `from` cursor (seq - 1) plus a
// `#c-<chunkId>` anchor. Mirrors the private tier's resolver, minus the
// campaign/viewpoint query params this tier's /chunks/{id} does not take.
import { error, redirect } from "@sveltejs/kit";
import { apiBase } from "$lib/server/grimoire-read.js";

export async function load({ params, fetch }) {
  const bookId = decodeURIComponent(params.book);
  const chunkId = params.chunk;

  const res = await fetch(
    `${apiBase()}/api/grimoire/chunks/${encodeURIComponent(chunkId)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  // 403 = the chunk belongs to a copyrighted book (an entity-mention "Sources"
  // link can point here). Redirect to the book page, which renders the locked
  // notice, rather than erroring on a broken-looking deep link.
  if (res.status === 403) {
    throw redirect(307, `/app/grimoire/book/${encodeURIComponent(bookId)}`);
  }
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "chunk not found");
  }
  const chunk = await res.json();

  // A null seq (nullable column, loader/backfill-set edge case) can't be
  // turned into a cursor; fall back to the book's first page rather than a
  // "None" cursor.
  const qs =
    chunk.seq != null
      ? `?from=${encodeURIComponent(String(chunk.seq - 1))}`
      : "";

  throw redirect(
    307,
    `/app/grimoire/book/${encodeURIComponent(bookId)}${qs}#c-${encodeURIComponent(chunkId)}`,
  );
}
