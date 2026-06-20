import { error } from "@sveltejs/kit";
import { version } from "$app/environment";
// Server-only imports (this is a .server.js): the manifest with full doc bodies
// and the marked-based renderer never reach the client. Only the rendered HTML
// for the requested doc and a small TOC are returned.
import manifest from "$lib/public/docs/docs-manifest.json";
import { buildPathIndex, getMeta, renderDoc } from "$lib/server/docs.js";
import { DOCS_CACHE_CONTROL } from "$lib/cache-headers.js";

// Built once per server process: slug -> entry lookup and the repo-path -> slug
// index used to rewrite intra-doc links.
const bySlug = new Map(manifest.map((e) => [e.slug, e]));
const pathIndex = buildPathIndex(manifest);

export function load({ params, setHeaders }) {
  const slug = (params.slug || "").replace(/\/+$/, "");
  const entry = bySlug.get(slug);
  if (!entry) throw error(404, "Documentation page not found");

  const { html, toc } = renderDoc(entry, pathIndex);

  setHeaders({
    "cache-control": DOCS_CACHE_CONTROL,
    // Build-versioned ETag: the doc HTML is a function of (content x build), and
    // content is baked into the build, so the version alone is a sound validator.
    etag: `"${version}-docs-${slug}"`,
  });

  return {
    slug,
    section: entry.section,
    title: entry.title,
    html,
    toc,
    meta: getMeta(entry),
  };
}
