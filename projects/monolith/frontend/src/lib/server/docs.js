// Server-only docs rendering for the public /docs route. Lives under
// $lib/server so SvelteKit guarantees it (and the docs manifest it operates on)
// never reaches a client bundle. Pure functions: the route loads import the
// manifest, call these, and ship only the rendered HTML + small nav structures.
//
// The markdown bodies are first-party, committed repo docs (reviewed in PRs).
// Even so we render with a constrained marked config: raw HTML blocks are
// escaped rather than passed through, intra-repo links are rewritten to /docs
// slugs, and links to docs that are NOT on the public allowlist are stripped to
// plain text (preserving ADR docs/001's link-stripping behaviour).

import { posix } from "node:path";
import { Marked } from "marked";

const escapeHtml = (s) =>
  String(s).replace(
    /[&<>]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c],
  );

const escapeAttr = (s) =>
  String(s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );

const stripTags = (s) => String(s).replace(/<[^>]*>/g, "");

// Strip the common inline markdown markers so a heading's slug id is derived
// from its plain text (matching what stripTags() yields from the rendered
// inline HTML, so the TOC anchor and the rendered <h*> id always agree).
function stripInlineMd(s) {
  return String(s)
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[_~]/g, "");
}

export function slugifyHeading(text) {
  const s = String(text)
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, "")
    .replace(/[\s_]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
  return s || "section";
}

// Matches a URL with a scheme (http:, mailto:, tel:, ...) or a protocol-relative
// (//host) prefix: those are external and left untouched.
const EXTERNAL = /^([a-z][a-z0-9+.-]*:|\/\/)/i;

const DECISIONS_DIR_PREFIX = "docs/";

// Map every doc's repo path -> its /docs slug, plus a directory alias for index
// and README docs so a link to `decisions/` or `projects/firecracker/` (the
// folder) resolves to the index/README page.
export function buildPathIndex(manifest) {
  const slugByPath = new Map();
  for (const e of manifest) {
    slugByPath.set(e.path, e.slug);
    if (e.path.endsWith("/index.md")) {
      slugByPath.set(e.path.slice(0, -"/index.md".length), e.slug);
    }
    if (e.path.endsWith("/README.md")) {
      slugByPath.set(e.path.slice(0, -"/README.md".length), e.slug);
    }
  }
  return slugByPath;
}

// Resolve a markdown link href authored relative to `fromPath` (the doc's repo
// path). Returns:
//   { href }                 - keep (external, in-page anchor, site-absolute, or
//                              a rewritten /docs slug). external:true for schemes.
//   null                     - strip the link (target not on the public allowlist).
export function resolveDocHref(href, fromPath, slugByPath) {
  if (!href) return { href: "" };
  if (href.startsWith("#")) return { href }; // in-page anchor
  if (EXTERNAL.test(href)) return { href, external: true };
  if (href.startsWith("/")) return { href }; // site-absolute

  // Relative intra-repo link: split off any #fragment / ?query before resolving.
  let rest = href;
  let hash = "";
  const hi = rest.indexOf("#");
  if (hi >= 0) {
    hash = rest.slice(hi);
    rest = rest.slice(0, hi);
  }
  const qi = rest.indexOf("?");
  if (qi >= 0) rest = rest.slice(0, qi);
  if (!rest) return { href: hash || href }; // was a pure anchor

  const dir = posix.dirname(fromPath || DECISIONS_DIR_PREFIX);
  const resolved = posix.normalize(posix.join(dir, rest));
  const slug = slugByPath.get(resolved);
  if (slug !== undefined) return { href: `/docs/${slug}${hash}` };
  return null; // not published -> strip to plain text
}

// Render a manifest entry to { html, toc }. A fresh Marked instance per call so
// the per-doc renderer closures (heading ids, link resolution) don't leak; docs
// are few and the output is CDN-cached, so the cost is irrelevant.
export function renderDoc(entry, slugByPath) {
  const m = new Marked({ gfm: true });
  const tokens = m.lexer(entry.content);

  // Pre-walk headings (document order, all depths) to assign de-duplicated ids,
  // and collect the depth 2-3 subset for the in-page TOC. Because the rendered
  // output is parsed from the SAME token list, the heading renderer can consume
  // these ids in order and stay in lockstep.
  const seen = new Map();
  const headingIds = [];
  const toc = [];
  for (const t of tokens) {
    if (t.type !== "heading") continue;
    const plain = stripInlineMd(t.text).trim();
    const base = slugifyHeading(plain);
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    const id = n === 0 ? base : `${base}-${n}`;
    headingIds.push(id);
    if (t.depth === 2 || t.depth === 3)
      toc.push({ depth: t.depth, text: plain, id });
  }

  let hIdx = 0;
  m.use({
    renderer: {
      heading({ tokens: hTokens, depth }) {
        const inner = this.parser.parseInline(hTokens);
        const id = headingIds[hIdx] ?? slugifyHeading(stripTags(inner));
        hIdx += 1;
        return `<h${depth} id="${id}">${inner}</h${depth}>\n`;
      },
      link({ href, title, tokens: lTokens }) {
        const text = this.parser.parseInline(lTokens);
        const r = resolveDocHref(href, entry.path, slugByPath);
        if (r === null) return text; // stripped: keep the display text only
        const t = title ? ` title="${escapeAttr(title)}"` : "";
        const ext = r.external
          ? ' target="_blank" rel="noopener noreferrer"'
          : "";
        return `<a href="${escapeAttr(r.href)}"${t}${ext}>${text}</a>`;
      },
      code({ text, lang }) {
        // marked passes the full info-string; keep only the language token. A
        // ```mermaid fence renders as a labelled source block by default (the
        // source stays escaped, as a no-JS fallback); the client marks it with
        // `doc-mermaid` so the docs page can render it to SVG lazily.
        const language = (lang || "").trim().split(/\s+/)[0];
        const cls = language ? ` class="language-${escapeAttr(language)}"` : "";
        const label = language ? ` data-lang="${escapeAttr(language)}"` : "";
        const mermaidCls = language === "mermaid" ? " doc-mermaid" : "";
        return `<pre class="doc-code${mermaidCls}"${label}><code${cls}>${escapeHtml(text)}</code></pre>\n`;
      },
      html({ text }) {
        // Neutralise raw HTML blocks rather than passing them through. The docs
        // are first-party, but this avoids any raw-HTML surprise on the public
        // surface; these reference docs do not rely on inline HTML.
        return escapeHtml(text);
      },
    },
  });

  const html = m.parser(tokens);
  return { html, toc };
}

const PROJECTS_PREFIX = "projects/";
const DECISIONS_PREFIX = "decisions/";

// Left-sidebar tree derived purely from the manifest (NOT from any VitePress
// adr-sidebar.json): project READMEs nested by path, then the decisions index
// and ADRs grouped by category. Carries titles/slugs only (no bodies), so it
// is safe to serialise to the client.
export function buildSidebar(manifest) {
  const sorted = [...manifest].sort((a, b) => a.order - b.order);
  const projects = [];
  const projectNodeByPath = new Map();
  let decisionsIndex = null;
  const catMap = new Map();
  for (const e of sorted) {
    if (e.section === "Projects") {
      const rel = e.slug.slice(PROJECTS_PREFIX.length);
      const parts = rel.split("/");
      let siblings = projects;
      let nodePath = "";
      for (let i = 0; i < parts.length; i++) {
        nodePath = nodePath ? `${nodePath}/${parts[i]}` : parts[i];
        let node = projectNodeByPath.get(nodePath);
        if (!node) {
          node = { name: parts[i], title: parts[i], slug: null, children: [] };
          projectNodeByPath.set(nodePath, node);
          siblings.push(node);
        }
        if (i === parts.length - 1) {
          node.title = e.title;
          node.slug = e.slug;
        }
        siblings = node.children;
      }
      continue;
    }
    if (e.slug === "decisions") {
      decisionsIndex = { slug: e.slug, title: e.title };
      continue;
    }
    const rest = e.slug.slice(DECISIONS_PREFIX.length);
    const cat = rest.includes("/") ? rest.slice(0, rest.indexOf("/")) : rest;
    if (!catMap.has(cat)) catMap.set(cat, []);
    catMap.get(cat).push({ slug: e.slug, title: e.title });
  }
  const categories = [...catMap.entries()].map(([name, items]) => ({
    name,
    items,
  }));
  return { projects, decisions: { index: decisionsIndex, categories } };
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
  const description = stripInlineMd(stripTags(para)).slice(0, 200).trim();
  return {
    title: entry.title,
    description: description || `${entry.title} - homelab documentation.`,
  };
}
