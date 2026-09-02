// Server-only docs rendering for the public /docs route. Lives under
// $lib/server so SvelteKit guarantees it (and the docs manifest it operates on)
// never reaches a client bundle. Pure functions: the route loads import the
// manifest, call these, and ship only the rendered HTML + small nav structures.
//
// The markdown bodies are first-party, committed repo docs (reviewed in PRs).
// Even so we render with a constrained marked config: raw HTML blocks are
// escaped rather than passed through, intra-repo links are rewritten to /docs
// slugs, and links to docs that are not on the public allowlist are stripped to
// plain text.

import { posix } from "node:path";
import { Marked } from "marked";
import { DOC_KINDS } from "$lib/public/docs/doc-kinds.js";
import { signedDocsImgUrl } from "./docs-img.js";

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

// Cut at the last word boundary within `max` chars and append "…" when the
// plain text is longer; never split a word. A single over-long token with no
// space in the window is sliced at `max` rather than returned whole.
function truncateAtWord(text, max) {
  if (text.length <= max) return text;
  const slice = text.slice(0, max);
  const at = slice.lastIndexOf(" ");
  const cut = at > 0 ? slice.slice(0, at) : slice;
  return `${cut}…`;
}

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

const REPO_DOCS_DIR = "docs/";

// Map every doc's repo path -> its /docs slug, plus a directory alias for index
// and README docs so a link to a published document's folder (with or without a
// trailing slash) resolves to that index or README page.
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

  const dir = posix.dirname(fromPath || REPO_DOCS_DIR);
  // posix.normalize keeps a trailing slash ("dir/" stays "dir/"), which would
  // miss the directory alias keyed without one.
  const resolved = posix.normalize(posix.join(dir, rest)).replace(/\/+$/, "");
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
      image({ href, title, text }) {
        if (
          href &&
          entry.figures &&
          Object.prototype.hasOwnProperty.call(entry.figures, href)
        ) {
          // Inserted as-is after gen_posts_manifest.py validates the SVG.
          const caption = text
            ? `<figcaption>${escapeHtml(text)}</figcaption>`
            : "";
          return `<figure class="fig"><div class="fig-art">${entry.figures[href]}</div>${caption}</figure>`;
        }
        // README images are authored as repo-relative refs (GitHub-native).
        // Resolve against the doc's repo path and serve via the signed
        // imgproxy URL over s3://docs-assets/ (objects keyed by repo path).
        // External absolute URLs pass through; anything unresolvable renders
        // as its alt text so a missing upload never yields a broken image.
        const alt = escapeAttr(stripInlineMd(text || ""));
        const t = title ? ` title="${escapeAttr(title)}"` : "";
        if (EXTERNAL.test(href || "")) {
          return `<img src="${escapeAttr(href)}" alt="${alt}"${t} loading="lazy" />`;
        }
        if (!href || href.startsWith("/") || href.startsWith("#")) {
          return text ? escapeHtml(text) : "";
        }
        const dir = posix.dirname(entry.path || ".");
        const repoPath = posix.normalize(posix.join(dir, href));
        if (repoPath.startsWith("..")) return text ? escapeHtml(text) : "";
        return `<img src="${escapeAttr(signedDocsImgUrl(repoPath))}" alt="${alt}"${t} loading="lazy" />`;
      },
      paragraph({ tokens: pTokens }) {
        const inner = this.parser.parseInline(pTokens);
        if (/^<figure class="fig">[\s\S]*<\/figure>$/.test(inner.trim())) {
          return `${inner}\n`;
        }
        return `<p>${inner}</p>\n`;
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
