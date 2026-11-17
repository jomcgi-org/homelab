// Build-only inputs: markdown, the manifest and Svelte figures never enter a
// browser bundle. Each virtual module contains just one rendered post.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { compile } from "svelte/compiler";
import { render } from "svelte/server";
import { renderDoc } from "../../monolith/frontend/src/lib/server/markdown.js";
import { splitFigures } from "../../monolith/frontend/src/lib/public/posts/ix/split.js";

const postsDir = fileURLToPath(
  new URL("../../monolith/frontend/src/lib/public/posts/", import.meta.url),
);
export const manifest = JSON.parse(
  readFileSync(resolve(postsDir, "posts-manifest.json"), "utf8"),
);
export const summaries = manifest.map(
  ({ content, figures, path, ...post }) => post,
);
const figureFiles = {
  fit: "Fit",
  residency: "Residency",
  context: "Context",
  "copy-queue": "CopyQueue",
  "draft-pays": "DraftPays",
  precision: "Precision",
  "prefix-race": "PrefixRace",
  concurrency: "Concurrency",
  published: "Published",
};

// Map the original sheet palette to the site palette. Local colour aliases are
// expanded here; every output colour is a checked palette.css token.
export const tokens = {
  ink: "color-text-primary",
  "ink-2": "color-text-secondary",
  "ink-3": "color-text-secondary",
  sheet: "color-background-surface",
  band: "color-background-muted",
  line: "color-border",
  stroke: "color-border-emphasized",
  "accent-ink": "color-text-accent",
  "font-ui": "font-family-body",
  "font-code": "font-family-code",
  "tone-gpu": "color-text-accent",
  "tone-ram": "color-blog-success",
  "tone-disk": "color-blog-warning",
  "tone-cache": "color-text-secondary",
  "tone-hot": "color-text-accent",
  "replay-warm": "color-blog-warning",
};
export function palette(css) {
  return css.replace(/var\(--([\w-]+)\)/g, (whole, name) =>
    tokens[name] ? `var(--${tokens[name]})` : whole,
  );
}
const hash = (value) =>
  createHash("sha256").update(value).digest("hex").slice(0, 12);
const styles = new Set();
const modules = new Map();
function figureModule(file) {
  if (modules.has(file)) return modules.get(file);
  const result = compile(readFileSync(file, "utf8"), {
    filename: file,
    generate: "server",
    css: "external",
  });
  let css = result.css?.code ?? "";
  // The figures use --t only as an alias selected by their tier class.
  css = css
    .replace(/--t:\s*var\(--([\w-]+)\);?/g, "color:var(--$1);")
    .replace(/var\(--t\)/g, "currentColor");
  css = css.replace(/var\(--axis-size\)/g, "12px");
  css = css.replace(/color-mix\([^;]+\)/g, "var(--color-background-muted)");
  styles.add(palette(css));
  const code = result.js.code.replace(
    /from\s+(["\x27])([^"\x27]+)\1/g,
    (_whole, _quote, specifier) => {
      const url = specifier.endsWith(".svelte")
        ? figureModule(resolve(dirname(file), specifier))
        : specifier.startsWith(".")
          ? pathToFileURL(resolve(dirname(file), specifier)).href
          : import.meta.resolve(specifier);
      return `from ${JSON.stringify(url)}`;
    },
  );
  const url = `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
  modules.set(file, url);
  return url;
}
function externalizeStyles(html) {
  // These are static numeric chart dimensions, known at build time. Move them
  // into the hashed stylesheet instead of relaxing style-src in the CSP.
  return html.replace(
    /<([a-z][^>]*?)\sstyle="([^"]*)"([^>]*)>/g,
    (_whole, before, style, after) => {
      style = style.replace(/--axis-size:\s*([^;]+)/g, "font-size:$1");
      if (!/^(?:(?:width|left|font-size):[\d.e%px\s+-]+;?)+$/.test(style))
        throw new Error(`Unexpected figure style: ${style}`);
      const name = `figure-${hash(style)}`;
      styles.add(`.${name}{${style}}`);
      let attrs = before + after;
      if (/\bclass="/.test(attrs))
        attrs = attrs.replace(/class="/, `class="${name} `);
      else attrs += ` class="${name}"`;
      return `<${attrs}>`;
    },
  );
}
let rendered;
async function posts() {
  if (rendered) return rendered;
  const figures = {};
  for (const [name, file] of Object.entries(figureFiles)) {
    const { default: Figure } = await import(
      figureModule(resolve(postsDir, "ix", `${file}.svelte`))
    );
    figures[name] = externalizeStyles((await render(Figure)).body);
  }
  rendered = manifest.map((entry) => {
    const { html, toc } = renderDoc(entry, new Map(), (path) => {
      throw new Error(`Blog image must be a committed figure: ${path}`);
    });
    const body = splitFigures(html)
      .map((part) =>
        part.ix ? (figures[part.ix] ?? part.fallback) : part.html,
      )
      .join("")
      .replace(
        /<div class="table-scroll">/g,
        '<div class="table-scroll" tabindex="0" role="group" aria-label="Data table">',
      );
    const { content, figures: _figures, path, ...metadata } = entry;
    return { ...metadata, html: body, toc };
  });
  return rendered;
}
export function blogContent() {
  return {
    name: "blog-content",
    async buildStart() {
      await posts();
    },
    resolveId(id) {
      if (id.startsWith("virtual:blog/")) return `\0${id}`;
    },
    async load(id) {
      if (!id.startsWith("\0virtual:blog/")) return;
      const name = id.slice("\0virtual:blog/".length);
      const all = await posts();
      if (name === "figures.css") return [...styles].join("\n");
      if (name === "figure-styles")
        return `export default ${JSON.stringify([...styles].join("\n"))}`;
      if (name === "index")
        return `export default ${JSON.stringify(summaries)}`;
      const post = all.find((post) => post.slug === name);
      if (!post) throw new Error(`Unknown blog slug: ${name}`);
      return `export default ${JSON.stringify(post)}`;
    },
  };
}
