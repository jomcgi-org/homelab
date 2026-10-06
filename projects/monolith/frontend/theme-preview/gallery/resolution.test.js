import { expect, it } from "vitest";
import { createServer } from "vite";
import { readFile, readdir, realpath } from "node:fs/promises";
import { resolve } from "node:path";

// Extract actual source imports so adding a new package subpath expands the check.
async function sourceImports() {
  const files = (await readdir("theme-preview/gallery")).filter(
    (name) => /\.(svelte|js)$/.test(name) && !name.endsWith(".test.js"),
  );
  const imports = new Map();
  for (const file of files) {
    const source = await readFile(`theme-preview/gallery/${file}`, "utf8");
    for (const match of source.matchAll(
      /\b(?:from\s*|import\s*)["'](@homelab\/design-system[^"']*)["']/g,
    ))
      imports.set(match[1], resolve(`theme-preview/gallery/${file}`));
  }
  return imports;
}

it("resolves every gallery package import identically under the real SvelteKit client and SSR config", async () => {
  // Do not inject resolution conditions into the real app configuration.
  const app = await createServer({
    configFile: resolve("vite.config.js"),
    optimizeDeps: { noDiscovery: true, include: [], exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  });
  const gallery = await createServer({
    configFile: resolve("theme-preview/vite.config.js"),
    optimizeDeps: { noDiscovery: true, include: [], exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  });
  try {
    const imports = await sourceImports();
    expect([...imports.keys()].sort()).toEqual([
      "@homelab/design-system/components",
      "@homelab/design-system/data-display",
      "@homelab/design-system/data-display/core",
      "@homelab/design-system/tokens/contract.css",
      "@homelab/design-system/tokens/technical-drawing.css",
    ]);
    for (const [specifier, importer] of imports) {
      const files = [];
      for (const ssr of [false, true]) {
        const actual = await app.pluginContainer.resolveId(
          specifier,
          importer,
          { ssr },
        );
        const isolated = await gallery.pluginContainer.resolveId(
          specifier,
          importer,
          { ssr },
        );
        expect(actual).not.toBeNull();
        expect(isolated).not.toBeNull();
        const actualFile = await realpath(actual.id);
        expect(await realpath(isolated.id)).toBe(actualFile);
        files.push(actualFile);
        if (specifier === "@homelab/design-system/data-display")
          expect(actualFile).toMatch(/\/data-display\/index\.js$/);
      }
      expect(files[1]).toBe(files[0]);
    }
    // Execute the component entry through the real app SSR graph, too. Resolving
    // only the isolated graph previously hid a production export failure.
    const entry = await app.ssrLoadModule(
      resolve("theme-preview/gallery/server.js"),
    );
    expect(entry.renderFixture().body).toContain(
      'data-gallery-section="dashboard"',
    );
    for (const runtime of ["svelte", "svelte/internal/client"]) {
      const fromFixture = await gallery.pluginContainer.resolveId(
        runtime,
        resolve("theme-preview/gallery/GalleryFixture.svelte"),
      );
      const fromPackage = await gallery.pluginContainer.resolveId(
        runtime,
        (
          await gallery.pluginContainer.resolveId(
            "@homelab/design-system/components",
            resolve("theme-preview/gallery/GalleryFixture.svelte"),
          )
        ).id,
      );
      expect(await realpath(fromPackage.id)).toBe(
        await realpath(fromFixture.id),
      );
    }
  } finally {
    await gallery.close();
    await app.close();
  }
});

it("keeps the gallery outside production sources and defines no local primitive", async () => {
  const files = await readdir("theme-preview/gallery");
  expect(files.filter((file) => file.endsWith(".svelte"))).toEqual([
    "GalleryFixture.svelte",
  ]);
  for (const file of files.filter((file) => /\.(js|svelte|html)$/.test(file))) {
    const source = await readFile(`theme-preview/gallery/${file}`, "utf8");
    expect(source).not.toMatch(
      /(?:from|import)\s*["'][^"']*(?:\.\.\/)+design-system\//,
    );
    expect(source).not.toMatch(/@import\s*["']@homelab/);
    if (!file.endsWith(".test.js"))
      expect(source).not.toMatch(
        /(?:from|import)\s*["'][^"']*(?:\/src\/|\$app\/)/,
      );
  }
  const fixture = await readFile(
    "theme-preview/gallery/GalleryFixture.svelte",
    "utf8",
  );
  expect(fixture).not.toMatch(/(?:export\s+let|\$:|var\(--(?!ds-))/);
  expect(fixture.match(/from\s*["'][^"']+\.svelte["']/g)).toBeNull();
  const build = await readFile("BUILD", "utf8");
  for (const name of ["src", "src_public"]) {
    const closure = build.split(`name = "${name}"`)[1].split("\njs_")[0];
    expect(closure).not.toContain("theme-preview/gallery");
  }
});
