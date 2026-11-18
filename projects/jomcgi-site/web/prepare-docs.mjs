// Turns the filtered manifest into independent HTML entries and page modules.
// Bazel supplies docs-manifest.json; prepare-local.mjs runs the same generator.
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import {
  buildPathIndex,
  buildProjectCards,
  buildSidebar,
  getMeta,
  renderDoc,
} from "../../monolith/frontend/src/lib/server/docs.js";

const escape = (s) =>
  String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );

export function htmlEntry({ title, description, slug }, script) {
  const canonical = `https://jomcgi.dev/docs/${slug}`;
  const fullTitle = `${title} · Docs · jomcgi.dev`;
  return `<!doctype html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>${escape(fullTitle)}</title>
<meta name="description" content="${escape(description)}" />
<link rel="canonical" href="${escape(canonical)}" />
<meta property="og:type" content="article" />
<meta property="og:site_name" content="Joe McGinley" />
<meta property="og:title" content="${escape(fullTitle)}" />
<meta property="og:description" content="${escape(description)}" />
<meta property="og:url" content="${escape(canonical)}" />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="${escape(fullTitle)}" />
<meta name="twitter:description" content="${escape(description)}" />
</head><body><div id="root"><!--ssr--></div>
<script type="module" src="/docs/_src/${script}.tsx"></script>
</body></html>\n`;
}

export async function prepareDocs(
  manifestFile = "docs-manifest.json",
  out = "docs",
) {
  const manifest = JSON.parse(await readFile(manifestFile, "utf8"));
  const slugs = manifest.map((entry) => entry.slug);
  if (
    new Set(slugs).size !== slugs.length ||
    slugs.some((s) => !/^[a-z0-9-]+(?:\/[a-z0-9-]+)?$/.test(s))
  ) {
    throw new Error("invalid or duplicate docs slug");
  }
  await rm(out, { recursive: true, force: true });
  const write = async (path, text) => {
    const file = join(out, path);
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, text);
  };
  const json = (path, value) => write(path, JSON.stringify(value));
  const sidebar = buildSidebar(manifest);
  await json("_src/sidebar.json", sidebar);
  await json("_src/projects.json", buildProjectCards(manifest));
  const paths = buildPathIndex(manifest);
  const imports = [];
  const pages = [];
  const search = [];
  for (const [i, entry] of manifest.entries()) {
    const id = `doc-${i}`;
    const name = `Doc${i}Page`;
    // Match the live route: the page owns the document-leading H1.
    const bodyEntry = {
      ...entry,
      content: entry.content.replace(/^(?:\r?\n)*#\s+[^\r\n]+(?:\r?\n)?/, ""),
    };
    const rendered = renderDoc(bodyEntry, paths, () => {
      // No current public doc uses repo images. Fail closed until an image is
      // added to the static build's inputs, rather than publishing a hub URL.
      throw new Error(`${entry.path}: add the image to the static docs build`);
    });
    // Keyboard users must be able to scroll wide code and tables, too.
    const html = rendered.html
      .replaceAll("<pre ", '<pre tabindex="0" ')
      .replaceAll(
        '<div class="table-scroll"',
        '<div tabindex="0" class="table-scroll"',
      );
    const { toc } = rendered;
    await json(`_src/${id}.json`, {
      slug: entry.slug,
      title: entry.title,
      project: entry.project,
      kind: entry.kind,
      toc,
      tabs: sidebar.find((p) => p.project === entry.project).tabs,
    });
    await json(`_src/${id}-body.json`, html);
    await write(
      `_src/${id}-page.tsx`,
      `import { DocsPage } from "../../src/docs-page";
import data from "./${id}.json";
import body from "./${id}-body.json";
export function ${name}() {
  // Vite removes the body import from the browser bundle. Hydration reuses
  // the HTML already delivered with this page, without a second download.
  const html = import.meta.env.SSR || import.meta.env.MODE === "test"
    ? body : document.getElementById("doc-body")!.innerHTML;
  return <DocsPage data={{ ...data, html }} />;
}\n`,
    );
    await write(
      `_src/${id}.tsx`,
      `import { ${name} } from "./${id}-page";
import { mount } from "../../src/mount";
mount(<${name} />);\n`,
    );
    await write(
      `${entry.slug}.html`,
      htmlEntry({ ...getMeta(entry), slug: entry.slug }, id),
    );
    imports.push(`import { ${name} } from "./${id}-page";`);
    pages.push(`${JSON.stringify(`docs/${entry.slug}.html`)}: ${name}`);
    search.push({
      slug: entry.slug,
      title: entry.title,
      project: entry.project,
      kind: entry.kind,
      text: `${entry.project} ${entry.kind} ${entry.title} ${entry.content}`.toLowerCase(),
    });
  }
  await write(
    "_src/pages.tsx",
    `${imports.join("\n")}\nexport const DOC_PAGES = {${pages.join(",\n")}};\n`,
  );
  await json("_src/search-index.json", search);
}

if (import.meta.url === `file://${process.argv[1]}`) await prepareDocs();
