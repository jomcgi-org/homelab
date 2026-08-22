import { error } from "@sveltejs/kit";
import { version } from "$app/environment";
import { DOCS_CACHE_CONTROL } from "$lib/cache-headers.js";
// Server-only imports: the manifest and markdown renderer do not reach the client.
import manifest from "$lib/public/posts/posts-manifest.json";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";

const bySlug = new Map(manifest.map((entry) => [entry.slug, entry]));
const emptyPathIndex = buildPathIndex([]);

export function load({ params, setHeaders }) {
  const entry = bySlug.get(params.slug);
  if (!entry) throw error(404, "Post not found");

  const { html } = renderDoc(entry, emptyPathIndex);
  setHeaders({
    "cache-control": DOCS_CACHE_CONTROL,
    etag: `"${version}-posts-${entry.slug}"`,
  });

  return {
    slug: entry.slug,
    title: entry.title,
    date: entry.date,
    summary: entry.summary,
    html,
  };
}
