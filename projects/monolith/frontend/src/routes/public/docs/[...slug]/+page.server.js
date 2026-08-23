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
import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";

export function load({ params, setHeaders }) {
  const slug = (params.slug || "").replace(/\/+$/, "");
  const entry = manifest.find((e) => e.slug === slug);
  if (!entry) throw error(404, "Documentation page not found");

  // The page owns the visible H1 so its project tabs can sit directly below
  // the title. Remove the first markdown H1 from the rendered body to avoid a
  // duplicate heading while preserving every other section and link.
  const bodyEntry = {
    ...entry,
    // Only the document-leading H1 (blank lines before it allowed). An H1
    // inside a leading code fence, or after any other line, stays in the body.
    content: entry.content.replace(/^(?:\r?\n)*#\s+[^\r\n]+(?:\r?\n)?/, ""),
  };
  const { html, toc } = renderDoc(bodyEntry, buildPathIndex(manifest));
  const project = buildSidebar(manifest).find(
    (p) => p.project === entry.project,
  );

  setHeaders({
    ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
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
