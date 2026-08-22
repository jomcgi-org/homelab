import { error } from "@sveltejs/kit";
import { version } from "$app/environment";
// Server-only imports (this is a .server.js): the manifest with full doc bodies
// and the marked-based renderer never reach the client. Only the rendered HTML
// for the requested doc and a small TOC are returned.
import manifest from "$lib/public/docs/docs-manifest.json";
import {
  buildPathIndex,
  buildSidebar,
  getMeta,
  renderDoc,
} from "$lib/server/docs.js";
import { DOCS_CACHE_CONTROL } from "$lib/cache-headers.js";

// Built once per server process: slug -> entry lookup and the repo-path -> slug
// index used to rewrite intra-doc links.
const bySlug = new Map(manifest.map((e) => [e.slug, e]));
const pathIndex = buildPathIndex(manifest);
const projectByName = new Map(
  buildSidebar(manifest).map((project) => [project.project, project]),
);

export function load({ params, setHeaders }) {
  const slug = (params.slug || "").replace(/\/+$/, "");
  const entry = bySlug.get(slug);
  if (!entry) throw error(404, "Documentation page not found");

  // The page owns the visible H1 so its project tabs can sit directly below
  // the title. Remove the first markdown H1 from the rendered body to avoid a
  // duplicate heading while preserving every other section and link.
  const bodyEntry = {
    ...entry,
    content: entry.content.replace(/^#\s+.+?\s*$(?:\r?\n)?/m, ""),
  };
  const { html, toc } = renderDoc(bodyEntry, pathIndex);
  const project = projectByName.get(entry.project);

  setHeaders({
    "cache-control": DOCS_CACHE_CONTROL,
    // Build-versioned ETag: the doc HTML is a function of (content x build), and
    // content is baked into the build, so the version alone is a sound validator.
    etag: `"${version}-docs-${slug}"`,
  });

  return {
    slug,
    project: entry.project,
    kind: entry.kind,
    tabs: project?.tabs ?? [],
    title: entry.title,
    html,
    toc,
    meta: getMeta(entry),
  };
}
