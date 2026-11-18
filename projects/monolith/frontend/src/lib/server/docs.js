// Docs navigation and metadata share the blog Markdown renderer.
// docs-hub.js supplies image signing only in the hub runtime.
import { DOC_KINDS } from "../public/docs/doc-kinds.js";
import { truncateAtWord, stripInlineMd, stripTags } from "./markdown.js";
export {
  buildPathIndex,
  resolveDocHref,
  slugifyHeading,
  renderDoc,
} from "./markdown.js";

// Flat project navigation derived from manifest order. It carries only project
// and tab metadata, never document bodies, so it is safe to serialise to the
// client.
export function buildSidebar(manifest) {
  const sorted = [...manifest].sort((a, b) => a.order - b.order);
  const entriesByProject = new Map();
  for (const e of sorted) {
    if (!entriesByProject.has(e.project)) entriesByProject.set(e.project, []);
    entriesByProject.get(e.project).push(e);
  }

  return [...entriesByProject.entries()].map(([project, entries]) => {
    const byKind = new Map(entries.map((entry) => [entry.kind, entry]));
    const readme = byKind.get("readme");
    const tabs = DOC_KINDS.flatMap(({ kind, label }) => {
      const entry = byKind.get(kind);
      return entry
        ? [
            {
              kind,
              label,
              slug: entry.slug,
              title: entry.title || label,
            },
          ]
        : [];
    });
    return {
      project,
      title: readme?.title || project,
      // Never the bare project name: a project with no README still has to
      // land on a real published tab.
      slug: readme?.slug || tabs[0]?.slug,
      tabs,
    };
  });
}

// SEO meta for a doc page: title + a description from the first prose paragraph.
export function getMeta(entry) {
  const lines = entry.content.split("\n");
  let para = "";
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (
      !line ||
      line.startsWith("#") ||
      line.startsWith("---") ||
      line.startsWith("|")
    ) {
      if (para) break;
      continue;
    }
    para += (para ? " " : "") + line;
    if (para.length > 200) break;
  }
  const description = truncateAtWord(
    stripInlineMd(stripTags(para)).trim(),
    200,
  );
  return {
    title: entry.title,
    description: description || `${entry.title} - homelab documentation.`,
  };
}

// Cards for the /docs overview: one per project, every kind listed with
// missing ones disabled. Lives here because SvelteKit rejects extra exports
// from +page.server.js.
export function buildProjectCards(entries) {
  const readmeByProject = new Map(
    entries
      .filter((entry) => entry.kind === "readme")
      .map((entry) => [entry.project, entry]),
  );

  return buildSidebar(entries).map((project) => {
    const readme = readmeByProject.get(project.project);
    const available = new Map(project.tabs.map((tab) => [tab.kind, tab]));
    return {
      project: project.project,
      title: project.title,
      slug: project.slug,
      excerpt: readme ? getMeta(readme).description : "",
      tabs: DOC_KINDS.map(({ kind, label }) => {
        const tab = available.get(kind);
        return {
          kind,
          label,
          slug: tab?.slug ?? null,
          title: tab?.title ?? label,
          disabled: !tab,
        };
      }),
    };
  });
}
