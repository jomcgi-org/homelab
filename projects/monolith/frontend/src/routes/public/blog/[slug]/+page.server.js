import { error } from "@sveltejs/kit";
import { version } from "$app/environment";
import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";
// Server-only imports: the manifest and markdown renderer do not reach the client.
import manifest from "$lib/public/posts/posts-manifest.json";
import { buildPathIndex, renderDoc } from "$lib/server/docs.js";

const bySlug = new Map(manifest.map((entry) => [entry.slug, entry]));
const emptyPathIndex = buildPathIndex([]);

export function load({ params, setHeaders }) {
  const entry = bySlug.get(params.slug);
  if (!entry) throw error(404, "Post not found");

  const { html, toc } = renderDoc(entry, emptyPathIndex);
  // Each numbered section is its own panel on the page, so split the
  // rendered body at the h2 boundaries the renderer emits. The chunk before
  // the first h2 (if any) stays with the head panel.
  const chunks = html.split(/(?=<h2 id=)/);
  const preamble = chunks[0]?.startsWith("<h2 ") ? "" : (chunks.shift() ?? "");
  const sections = chunks.filter((c) => c.trim());
  setHeaders({
    ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
    etag: `"${version}-blog-${entry.slug}"`,
  });

  return {
    slug: entry.slug,
    title: entry.title,
    date: entry.date,
    summary: entry.summary,
    tags: entry.tags,
    html,
    preamble,
    sections,
    // Sections (depth 2) with their subsections nested, for the spine.
    toc: toc.reduce((acc, h) => {
      if (h.depth === 2) acc.push({ ...h, children: [] });
      else if (acc.length) acc[acc.length - 1].children.push(h);
      return acc;
    }, []),
  };
}
