// Builds the site, then writes each page's server-rendered HTML into its built
// entry. A page reads fully, with its real preview tags, before any script
// runs; the browser then hydrates the same tree (HOME-178 performance contract).
// Usage: node build.mjs (from this directory)
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build, createServer } from "vite";
import { renderDocsDiagrams } from "./render-diagrams.mjs";

import { personLdScript } from "../../monolith/frontend/src/lib/public/seo.js";
import { hub, posts } from "./site-inputs.mjs";
import { robots, llms, webmanifest } from "./machine-readable.mjs";

await build();
// Vite's SSR loader returns source asset URLs. Resolve them against the client
// build, including React's image preload, so no-JS pages and hydration agree.
const assets = JSON.parse(await readFile("dist/.vite/manifest.json", "utf8"));
function assetUrls(html) {
  return html.replace(
    /(src|href)="\/(src\/[^"]+)"/g,
    (_, attribute, source) => {
      const file = assets[source]?.file;
      if (!file) throw new Error(`No built asset for ${source}`);
      return `${attribute}="/${file}"`;
    },
  );
}
for (const [name, content] of Object.entries({
  "robots.txt": robots(),
  "llms.txt": llms(posts),
  "manifest.webmanifest": JSON.stringify(webmanifest()),
})) {
  await writeFile(join("dist", name), content);
}
for (const name of ["favicon.svg", "apple-touch-icon.png"]) {
  await copyFile(new URL(`static/${name}`, hub), join("dist", name));
}
await copyFile(new URL("preview/fonts/OFL.txt", hub), "dist/assets/OFL.txt");

// Load prerender.tsx through Vite, as the tests do, without a second bundle.
const server = await createServer({
  appType: "custom",
  logLevel: "warn",
  cacheDir: join(tmpdir(), "jomcgi-site-vite"),
  optimizeDeps: {
    noDiscovery: true,
    include: [],
    esbuildOptions: { target: "es2022" },
  },
  server: { middlewareMode: true, hmr: false, watch: null },
});
try {
  const { MARK, render } = await server.ssrLoadModule("/src/prerender.tsx");
  const { marketplace } = await server.ssrLoadModule("/src/scotland-data.ts");
  await mkdir("dist/scotland", { recursive: true });
  await writeFile(
    "dist/scotland/marketplace.json",
    `${JSON.stringify(marketplace, null, 2)}\n`,
  );
  const { PAGES } = await server.ssrLoadModule("/src/pages.tsx");
  const { sitemap } = await server.ssrLoadModule("/src/sitemap.ts");
  await writeFile("dist/sitemap.xml", sitemap(Object.keys(PAGES)));
  for (const entry of Object.keys(PAGES)) {
    const file = join("dist", entry);
    const parts = (await readFile(file, "utf8")).split(MARK);
    if (parts.length !== 2) {
      throw new Error(`${entry} must hold exactly one ${MARK}`);
    }
    const html = parts.join(assetUrls(render(entry)));
    await writeFile(
      file,
      entry === "index.html"
        ? html.replace("</head>", `${personLdScript}</head>`)
        : html,
    );
  }
} finally {
  await server.close();
}

await renderDocsDiagrams();
