// Chromium and the renderer bundle are build inputs only. No browser binary,
// Mermaid code or temporary styles are published with the finished diagrams.
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { build } from "vite";
import { withSiteBrowser } from "./browser.mjs";

export async function renderDocsDiagrams() {
  const manifest = JSON.parse(await readFile("docs-manifest.json", "utf8"));
  const entries = manifest.filter(({ content }) =>
    /^```mermaid\s*$/m.test(content),
  );
  if (!entries.length) return;
  const temporary = await mkdtemp(join(tmpdir(), "docs-diagrams-"));
  try {
    await build({
      configFile: false,
      logLevel: "warn",
      build: {
        target: "es2022",
        outDir: temporary,
        lib: {
          entry: resolve("src/docs-diagrams.ts"),
          formats: ["es"],
          fileName: () => "render.js",
        },
      },
    });
    await withSiteBrowser(
      { renderer: temporary, csp: false, scripts: false },
      async (page, origin) => {
        for (const { slug } of entries) {
          await page.goto(`${origin}/docs/${slug}`, {
            waitUntil: "networkidle0",
          });
          const rendered = await page.evaluate(async (origin) => {
            await document.fonts.ready;
            const { renderDiagrams } = await import(
              `${origin}/_renderer/render.js`
            );
            return await renderDiagrams();
          }, origin);
          const file = `dist/docs/${slug}.html`;
          const html = await readFile(file, "utf8");
          const updated = html.replace(
            /<div id="doc-body" class="docs-prose">[\s\S]*?<\/div>(?=<\/article>)/,
            () => rendered,
          );
          if (updated === html)
            throw new Error(`${slug}: missing rendered diagrams`);
          await writeFile(file, updated);
        }
      },
    );
    console.log(`Rendered diagrams in ${entries.length} docs pages`);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}
